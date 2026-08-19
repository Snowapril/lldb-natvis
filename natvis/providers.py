"""Generic synthetic-children provider driven by the natvis <Expand> model."""

import lldb

from . import interp, log
from .display import interpolate
from .expr import (EvalError, evaluate, evaluate_any, evaluate_bool,
                   evaluate_int, make_value, subst_index)
from .model import (
    ArrayItemsNode, CustomListItemsNode, ExpandedItemNode, IndexListItemsNode,
    ItemNode, LinkedListItemsNode, SyntheticNode, TreeItemsNode,
)
from .registry import REGISTRY


def _valid(sbv):
    return sbv is not None and sbv.IsValid() and sbv.GetError().Success()


def _nonnull_ptr(sbv):
    return _valid(sbv) and sbv.GetValueAsUnsigned(0) != 0


class _Entry:
    """One child slot: a display name plus a lazy realizer."""
    __slots__ = ("name", "thunk", "cached")

    def __init__(self, name, thunk):
        self.name = name
        self.thunk = thunk
        self.cached = None

    def realize(self):
        if self.cached is None:
            try:
                self.cached = self.thunk()
            except Exception as exc:     # must never escape into LLDB
                log.debug("child %r failed: %s", self.name, exc)
                self.cached = lldb.SBValue()
        return self.cached


class NatvisSyntheticProvider:
    def __init__(self, valobj, internal_dict):
        self.val = valobj.GetNonSyntheticValue()
        self.entries = None      # None => passthrough to raw children
        self.name_index = {}

    # -------------------------------------------------- provider protocol

    def update(self):
        self.entries = None
        self.name_index = {}
        try:
            resolved = REGISTRY.resolve_for_value(self.val)
            # a <Synthetic> pseudo-member with no nested <Expand> is a leaf:
            # build (empty) entries rather than falling back to raw children
            if resolved is not None and (resolved.expand() or
                                         resolved.synthetic is not None):
                self.entries = _Builder(resolved, resolved.val).build()
                self.name_index = {e.name: i
                                   for i, e in enumerate(self.entries)}
        except Exception as exc:            # never break the debugger
            log.warning("synthetic update failed for %s: %s",
                        self.val.GetTypeName(), exc)
            self.entries = None
        return False

    def num_children(self):
        if self.entries is None:
            return self.val.GetNumChildren()
        return len(self.entries)

    def has_children(self):
        return self.num_children() > 0

    def get_child_index(self, name):
        if self.entries is None:
            return self.val.GetIndexOfChildWithName(name)
        idx = self.name_index.get(name)
        if idx is not None:
            return idx
        if name.startswith("[") and name.endswith("]"):
            try:
                candidate = int(name[1:-1])
                if 0 <= candidate < len(self.entries):
                    return candidate
            except ValueError:
                pass
        return -1

    def get_child_at_index(self, index):
        try:
            if self.entries is None:
                return self.val.GetChildAtIndex(index)
            if index < 0 or index >= len(self.entries):
                return lldb.SBValue()
            entry = self.entries[index]
            child = entry.realize()
            return self._renamed(entry.name, child)
        except Exception as exc:         # must never escape into LLDB
            log.debug("get_child_at_index(%d) failed: %s", index, exc)
            return lldb.SBValue()

    # ------------------------------------------------------------ helpers

    def _renamed(self, name, sbv):
        if not _valid(sbv) or sbv.GetName() == name:
            return sbv
        addr = sbv.GetLoadAddress()
        if addr != lldb.LLDB_INVALID_ADDRESS:
            out = self.val.CreateValueFromAddress(name, addr, sbv.GetType())
            if _valid(out):
                return out
        data = sbv.GetData()
        if data.IsValid():
            out = self.val.CreateValueFromData(name, data, sbv.GetType())
            if _valid(out):
                return out
        return sbv


class _Builder:
    def __init__(self, resolved, val):
        self.resolved = resolved
        self.val = val
        self.cap = REGISTRY.max_children
        self.entries = []

    def _prep(self, expr):
        return self.resolved.prep(expr)

    def _cond_ok(self, node):
        if not node.allows(""):
            return False
        if node.condition is None:
            return True
        try:
            return evaluate_bool(self.val, self._prep(node.condition))
        except EvalError as exc:
            log.debug("Expand condition %r: %s", node.condition, exc)
            return False

    def _pick(self, cond_exprs, env=None):
        """First sibling element (e.g. <Size>) whose Condition holds."""
        for ce in cond_exprs:
            if ce.condition is not None:
                try:
                    if not evaluate_bool(self.val, self._prep(ce.condition),
                                         env):
                        continue
                except EvalError:
                    continue
            return ce
        return None

    def build(self):
        for node in self.resolved.expand():
            if len(self.entries) >= self.cap:
                break
            try:
                if not self._cond_ok(node):
                    continue
                if isinstance(node, ItemNode):
                    self._build_item(node)
                elif isinstance(node, ArrayItemsNode):
                    self._build_array(node)
                elif isinstance(node, IndexListItemsNode):
                    self._build_index_list(node)
                elif isinstance(node, LinkedListItemsNode):
                    self._build_linked_list(node)
                elif isinstance(node, TreeItemsNode):
                    self._build_tree(node)
                elif isinstance(node, ExpandedItemNode):
                    self._build_expanded(node)
                elif isinstance(node, SyntheticNode):
                    self._build_synthetic(node)
                elif isinstance(node, CustomListItemsNode):
                    self._build_custom(node)
            except EvalError as exc:
                if not node.optional:
                    log.debug("Expand node failed: %s", exc)
        return self.entries[:self.cap]

    # ------------------------------------------------------------- nodes

    def _build_item(self, node):
        expr = self._prep(node.expr)
        name = node.name or expr
        try:
            sbv = self._value_of(expr, name)
        except EvalError:
            if node.optional:
                return
            raise
        self.entries.append(_Entry(name, lambda sbv=sbv: sbv))

    def _value_of(self, expr, name, env=None):
        """Evaluate to an SBValue, materializing computed numbers locally
        instead of paying for a Tier-2 compile just to get a scalar."""
        value, is_sbvalue = evaluate_any(self.val, expr, env)
        return value if is_sbvalue else make_value(self.val, name, value)

    def _size_of(self, sizes, env=None, dim=None):
        ce = self._pick(sizes, env)
        if ce is None:
            return None
        expr = self._prep(ce.expr)
        if dim is not None:
            expr = subst_index(expr, dim)
        try:
            # signed + clamp: a garbage/sentinel negative count must yield
            # zero children, not 2^64-ish (VS shows zero items too)
            return max(0, evaluate_int(self.val, expr, env, signed=True))
        except EvalError:
            if ce.optional:
                return None
            raise

    def _build_array(self, node):
        ce = self._pick(node.value_pointers)
        if ce is None:
            return
        base = evaluate(self.val, self._prep(ce.expr))
        t = base.GetType()
        if t.IsPointerType():
            elem_type = t.GetPointeeType()
            base_addr = base.GetValueAsUnsigned(0)
        elif t.IsArrayType():
            elem_type = t.GetArrayElementType()
            base_addr = base.GetLoadAddress()
        else:
            elem_type = t
            base_addr = base.GetLoadAddress()
        if not elem_type.IsValid() or elem_type.GetByteSize() == 0:
            return
        if not base_addr or base_addr == lldb.LLDB_INVALID_ADDRESS:
            return
        rank = 1
        if node.rank_expr:
            rank = evaluate_int(self.val, self._prep(node.rank_expr)) or 1
        dims = []
        for d in range(rank):
            size = self._size_of(node.sizes, dim=d if rank > 1 else None)
            if size is None:
                return
            dims.append(max(0, size))
        lower = 0
        if node.lower_bound_expr:
            lower = evaluate_int(self.val, self._prep(node.lower_bound_expr),
                                 signed=True)
        total = 1
        for d in dims:
            total *= d
        total = min(total, self.cap - len(self.entries))
        esize = elem_type.GetByteSize()
        forward = node.direction != "Backward"
        for flat in range(total):
            indices = self._unflatten(flat, dims, forward)
            if rank > 1:
                name = "[%s]" % ",".join(str(i + lower) for i in indices)
            else:
                name = "[%d]" % (indices[0] + lower)
            addr = base_addr + flat * esize
            self.entries.append(_Entry(
                name,
                lambda name=name, addr=addr, et=elem_type:
                    self.val.CreateValueFromAddress(name, addr, et)))

    @staticmethod
    def _unflatten(flat, dims, forward):
        indices = []
        if forward:  # row-major: last dim varies fastest
            for d in reversed(dims):
                indices.append(flat % d if d else 0)
                flat //= max(d, 1)
            indices.reverse()
        else:        # column-major: first dim varies fastest
            for d in dims:
                indices.append(flat % d if d else 0)
                flat //= max(d, 1)
        return indices

    def _build_index_list(self, node):
        size = self._size_of(node.sizes)
        if size is None:
            return
        size = min(size, self.cap - len(self.entries))
        for i in range(size):
            def thunk(i=i, node=node):
                for ce in node.value_nodes:
                    if ce.condition is not None:
                        cond = subst_index(self._prep(ce.condition), i)
                        try:
                            if not evaluate_bool(self.val, cond):
                                continue
                        except EvalError:
                            continue
                    expr = subst_index(self._prep(ce.expr), i)
                    return evaluate(self.val, expr)
                raise EvalError("no ValueNode matched for index %d" % i)
            self.entries.append(_Entry("[%d]" % i, thunk))

    def _node_eval(self, node_ptr, expr):
        """Evaluate an expression relative to a list/tree node pointer."""
        expr = expr.strip()
        node_val = node_ptr.Dereference()
        if not _valid(node_val):
            raise EvalError("cannot dereference node")
        if expr == "this":
            return node_val
        return evaluate(node_val, expr)

    def _emit_node_value(self, node_ptr, value_node, name_attr, cond, index):
        if cond is not None:
            try:
                if not evaluate_bool(node_ptr.Dereference(),
                                     self._prep(cond)):
                    return False
            except EvalError:
                return False
        sbv = self._node_eval(node_ptr, self._prep(value_node or "this"))
        if name_attr:
            name = interpolate(name_attr, node_ptr.Dereference(),
                               self.resolved.bindings,
                               self.resolved.intrinsics())
        else:
            name = "[%d]" % index
        self.entries.append(_Entry(name, lambda sbv=sbv: sbv))
        return True

    def _head_pointer(self, expr):
        expr = self._prep(expr).strip()
        if expr == "this":
            head = self.val.AddressOf()
        else:
            head = evaluate(self.val, expr)
        return head

    def _build_linked_list(self, node):
        size = self._size_of(node.sizes)
        bound = min(size if size is not None else self.cap,
                    self.cap - len(self.entries))
        cur = self._head_pointer(node.head_pointer)
        visited = set()
        emitted = 0
        steps = self.cap * 64   # walk-length budget: a filtering ValueNode
        while _nonnull_ptr(cur) and emitted < bound and steps > 0:
            steps -= 1          # Condition must not turn this into an
            addr = cur.GetValueAsUnsigned(0)   # unbounded chain traversal
            if addr in visited:
                break
            visited.add(addr)
            if self._emit_node_value(cur, node.value_node,
                                     node.value_node_name,
                                     node.value_node_condition, emitted):
                emitted += 1
            cur = self._node_eval(cur, self._prep(node.next_pointer))

    def _build_tree(self, node):
        size = self._size_of(node.sizes)
        bound = min(size if size is not None else self.cap,
                    self.cap - len(self.entries))
        cur = self._head_pointer(node.head_pointer)
        stack = []
        visited = set()
        emitted = 0
        steps = self.cap * 64   # walk-length budget (see _build_linked_list)
        while (stack or _nonnull_ptr(cur)) and emitted < bound and steps > 0:
            steps -= 1
            while _nonnull_ptr(cur) and steps > 0:
                steps -= 1
                addr = cur.GetValueAsUnsigned(0)
                if addr in visited:
                    cur = lldb.SBValue()
                    break
                visited.add(addr)
                stack.append(cur)
                try:
                    cur = self._node_eval(cur, self._prep(node.left_pointer))
                except EvalError:
                    cur = lldb.SBValue()
            if not stack:
                break
            top = stack.pop()
            if self._emit_node_value(top, node.value_node,
                                     node.value_node_name,
                                     node.value_node_condition, emitted):
                emitted += 1
            try:
                cur = self._node_eval(top, self._prep(node.right_pointer))
            except EvalError:
                cur = lldb.SBValue()

    def _build_expanded(self, node):
        try:
            sbv = evaluate(self.val, self._prep(node.expr))
        except EvalError:
            if node.optional:
                return
            raise
        if sbv.GetType().IsPointerType():
            if not _nonnull_ptr(sbv):
                return
            sbv = sbv.Dereference()
        if not _valid(sbv):
            return
        same_object = (
            sbv.GetLoadAddress() == self.val.GetLoadAddress() and
            sbv.GetLoadAddress() != lldb.LLDB_INVALID_ADDRESS and
            sbv.GetType().GetCanonicalType().GetName() ==
            self.val.GetType().GetCanonicalType().GetName())
        source = self.val if same_object else sbv
        if not same_object:
            synthetic = source.GetSyntheticValue() \
                if hasattr(source, "GetSyntheticValue") else None
            if synthetic is not None and synthetic.IsValid() and \
                    synthetic.IsSynthetic():
                source = synthetic
        count = min(source.GetNumChildren(), self.cap - len(self.entries))
        for i in range(count):
            child = source.GetChildAtIndex(i)
            if not child.IsValid():
                continue
            self.entries.append(_Entry(child.GetName() or "[%d]" % i,
                                       lambda c=child: c))

    def _build_synthetic(self, node):
        addr = self.val.GetLoadAddress()
        name = node.name or "<synthetic>"
        if node.expression:
            try:
                sbv = evaluate(self.val, self._prep(node.expression))
            except EvalError:
                if node.optional:
                    return
                raise
            self.entries.append(_Entry(name, lambda sbv=sbv: sbv))
            return
        if addr == lldb.LLDB_INVALID_ADDRESS:
            return
        REGISTRY.register_synthetic_override(
            addr, name, self.val.GetType().GetName() or "",
            node, self.resolved.ntype, self.resolved.bindings)
        t = self.val.GetType()
        self.entries.append(_Entry(
            name,
            lambda name=name, addr=addr, t=t:
                self.val.CreateValueFromAddress(name, addr, t)))

    def _build_custom(self, node):
        items = interp.run(node, self.resolved,
                           self.cap - len(self.entries))
        for name, sbv in items:
            self.entries.append(_Entry(name, lambda sbv=sbv: sbv))
