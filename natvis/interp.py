"""CustomListItems mini-interpreter: Variable / Size / Loop / Break / If /
Elseif / Else / Exec / Item, with a pure-Python integer fast path for the
statements that run O(N) times."""

import re

import lldb

from . import log
from .display import interpolate
from .expr import (EvalError, evaluate, evaluate_any, evaluate_bool,
                   evaluate_int, int_eval, make_value)
from .model import SBreak, SExec, SIf, SItem, SLoop, SSize

_LOOP_CAP = 100000
_GLOBAL_STEP_CAP = 500000   # shared by all nested loops of one program

_ASSIGN_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(=|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=)\s*(.*?)\s*$", re.S)
_INCDEC_RE = re.compile(
    r"^\s*(?:(\+\+|--)\s*([A-Za-z_][A-Za-z0-9_]*)"
    r"|([A-Za-z_][A-Za-z0-9_]*)\s*(\+\+|--))\s*$")

_INT_BASIC_TYPES = frozenset([
    lldb.eBasicTypeBool, lldb.eBasicTypeChar, lldb.eBasicTypeSignedChar,
    lldb.eBasicTypeUnsignedChar, lldb.eBasicTypeShort,
    lldb.eBasicTypeUnsignedShort, lldb.eBasicTypeInt,
    lldb.eBasicTypeUnsignedInt, lldb.eBasicTypeLong,
    lldb.eBasicTypeUnsignedLong, lldb.eBasicTypeLongLong,
    lldb.eBasicTypeUnsignedLongLong, lldb.eBasicTypeChar16,
    lldb.eBasicTypeChar32, lldb.eBasicTypeWChar,
])


def _is_integral(sbv):
    basic = sbv.GetType().GetCanonicalType().GetBasicType()
    return basic in _INT_BASIC_TYPES


def _to_py(sbv):
    if _is_integral(sbv):
        return sbv.GetValueAsSigned(0)
    return sbv


class _BreakLoop(Exception):
    pass


class _Done(Exception):
    pass


class Interp:
    def __init__(self, node, resolved, cap):
        self.node = node
        self.resolved = resolved
        self.val = resolved.val
        self.env = {}
        self.items = []          # (name, SBValue)
        self.max_items = min(cap, node.max_items or cap)
        self.steps = 0

    def _prep(self, expr):
        return self.resolved.prep(expr)

    def _cond(self, cond):
        if cond is None:
            return True
        try:
            return evaluate_bool(self.val, self._prep(cond), self.env)
        except EvalError as exc:
            log.debug("CustomListItems condition %r: %s", cond, exc)
            return False

    def run(self):
        for var in self.node.variables:
            try:
                init = self._prep(var.init)
                try:
                    self.env[var.name] = int_eval(init, self.env)
                except EvalError:
                    self.env[var.name] = _to_py(
                        evaluate(self.val, init, self.env))
            except EvalError as exc:
                log.debug("CustomListItems <Variable %s>: %s", var.name, exc)
                self.env[var.name] = 0
        try:
            self._exec_block(self.node.block)
        except _Done:
            pass
        except _BreakLoop:
            pass
        except EvalError as exc:
            log.debug("CustomListItems aborted: %s", exc)
        return self.items

    def _exec_block(self, block):
        for stmt in block:
            self.steps += 1
            if self.steps > _GLOBAL_STEP_CAP:
                log.warning("CustomListItems global step cap hit")
                raise _Done()
            if isinstance(stmt, SItem):
                if not self._cond(stmt.condition):
                    continue
                self._emit(stmt)
            elif isinstance(stmt, SExec):
                if not self._cond(stmt.condition):
                    continue
                self._exec_assign(stmt.code)
            elif isinstance(stmt, SIf):
                for cond, body in stmt.branches:
                    if cond is None or self._cond(cond):
                        self._exec_block(body)
                        break
            elif isinstance(stmt, SLoop):
                iterations = 0
                try:
                    while self._cond(stmt.condition):
                        iterations += 1
                        if iterations > _LOOP_CAP:
                            log.warning("CustomListItems loop cap hit")
                            break
                        self._exec_block(stmt.body)
                except _BreakLoop:
                    pass
            elif isinstance(stmt, SBreak):
                if self._cond(stmt.condition):
                    raise _BreakLoop()
            elif isinstance(stmt, SSize):
                if not self._cond(stmt.condition):
                    continue
                try:
                    declared = evaluate_int(
                        self.val, self._prep(stmt.expr), self.env)
                    self.max_items = min(self.max_items, declared)
                    if len(self.items) >= self.max_items:
                        raise _Done()
                except EvalError:
                    pass

    def _emit(self, stmt):
        try:
            value, is_sbvalue = evaluate_any(
                self.val, self._prep(stmt.expr), self.env)
        except EvalError as exc:
            log.debug("CustomListItems <Item> %r: %s", stmt.expr, exc)
            return
        if stmt.name:
            name = interpolate(stmt.name, self.val, self.resolved.bindings,
                               self.resolved.intrinsics(), self.env)
        else:
            name = "[%d]" % len(self.items)
        if not is_sbvalue:
            try:
                value = make_value(self.val, name, value)
            except EvalError as exc:
                log.debug("CustomListItems <Item> %r: %s", stmt.expr, exc)
                return
        self.items.append((name, value))
        if len(self.items) >= self.max_items:
            raise _Done()

    def _exec_assign(self, code):
        m = _INCDEC_RE.match(code)
        if m:
            name = m.group(2) or m.group(3)
            op = m.group(1) or m.group(4)
            delta = 1 if op == "++" else -1
            cur = self.env.get(name)
            if isinstance(cur, int):
                self.env[name] = cur + delta
                return
            code = "%s = %s + %d" % (name, name, delta)
        m = _ASSIGN_RE.match(code)
        if not m:
            # a silently skipped assignment would leave the enclosing <Loop>
            # condition unchanged and spin it to the cap: abort instead
            raise EvalError("CustomListItems <Exec> unsupported: %r" % code)
        name, op, rhs = m.group(1), m.group(2), m.group(3)
        if op != "=":
            rhs = "%s %s (%s)" % (name, op[:-1], rhs)
        rhs = self._prep(rhs)
        try:
            self.env[name] = int_eval(rhs, self.env)
            return
        except EvalError:
            pass
        try:
            self.env[name] = _to_py(evaluate(self.val, rhs, self.env))
        except EvalError as exc:
            log.debug("CustomListItems <Exec> %r: %s", code, exc)


def run(node, resolved, cap):
    """Execute a <CustomListItems> program. Returns [(name, SBValue)]."""
    return Interp(node, resolved, cap).run()
