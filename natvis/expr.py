"""Natvis expression preprocessing and two-tier evaluation.

Tier 1 walks SBValue children directly (no compiler) for the member/deref/index
chains that dominate real natvis files.  Tier 2 falls back to
SBValue.EvaluateExpression, which evaluates C++ with the object's members in
scope -- exactly the natvis semantic."""

import ast
import math
import re
from functools import lru_cache

import lldb

from . import log


class EvalError(Exception):
    pass


# ------------------------------------------------------------- preprocessing

_TPARAM_RE = re.compile(r"\$T(\d+)")
_IDX_RE = re.compile(r"\$i\b")
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def subst_index(expr, i):
    return _IDX_RE.sub(str(i), expr)


def _subst_tparams(expr, bindings):
    def repl(m):
        n = int(m.group(1))
        if n < 1 or n > len(bindings):
            raise EvalError("no binding for $T%d in %r" % (n, expr))
        return bindings[n - 1]
    return _TPARAM_RE.sub(repl, expr)


def _find_close_paren(s, open_idx):
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_args(s):
    if not s.strip():
        return []
    args = []
    depth = 0
    start = 0
    for i, ch in enumerate(s):
        if ch in "(<[":
            depth += 1
        elif ch in ")>]":
            if ch == ">" and i > 0 and s[i - 1] == "-":
                continue
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(s[start:i].strip())
            start = i + 1
    args.append(s[start:].strip())
    return args


def _expand_intrinsics(expr, intr_map):
    """Expand intrinsic call sites textually.  Rescanning from the insertion
    point lets intrinsics call other intrinsics, but a hard substitution
    budget bounds (mutually) recursive definitions, which would otherwise
    grow the string forever inside an LLDB formatter callback."""
    if not intr_map:
        return expr
    out = expr
    pos = 0
    budget = 64
    while True:
        m = _CALL_RE.search(out, pos)
        if not m:
            break
        name = m.group(1)
        if name not in intr_map:
            pos = m.end()
            continue
        # intrinsics are free functions: `obj.size()` / `p->size()` /
        # `ns::size()` must not expand an <Intrinsic Name="size">
        before = out[m.start() - 1] if m.start() > 0 else ""
        if before in (".", ":") or out[m.start() - 2:m.start()] == "->":
            pos = m.end()
            continue
        open_idx = m.end() - 1
        close_idx = _find_close_paren(out, open_idx)
        if close_idx < 0:
            pos = m.end()
            continue
        body, params = intr_map[name]
        args = _split_args(out[open_idx + 1:close_idx])
        if len(args) != len(params):
            pos = m.end()
            continue
        if budget == 0:
            raise EvalError("intrinsic expansion budget exceeded in %r "
                            "(recursive <Intrinsic>?)" % expr)
        budget -= 1
        expanded = body
        for (pname, _ptype), arg in zip(params, args):
            expanded = re.sub(r"\b%s\b" % re.escape(pname),
                              "(" + arg + ")", expanded)
        out = out[:m.start()] + "(" + expanded + ")" + out[close_idx + 1:]
        pos = m.start()
    return out


@lru_cache(maxsize=8192)
def _preprocess_cached(expr, bindings, intr_key):
    if bindings:
        expr = _subst_tparams(expr, bindings)
    if intr_key:
        intr_map = {name: (body, params) for (name, body, params) in intr_key}
        expr = _expand_intrinsics(expr, intr_map)
    return expr


def preprocess(expr, bindings=(), intrinsics=None):
    intr_key = ()
    if intrinsics:
        intr_key = tuple((i.name, i.expression, tuple(i.params))
                         for i in intrinsics)
    return _preprocess_cached(expr, tuple(bindings), intr_key)


# ------------------------------------------------------ pure-int fast path

_PY_XLATE = [
    (re.compile(r"&&"), " and "),
    (re.compile(r"\|\|"), " or "),
    (re.compile(r"!(?![=])"), " not "),
    (re.compile(r"\btrue\b"), "True"),
    (re.compile(r"\bfalse\b"), "False"),
    (re.compile(r"\bnullptr\b"), "0"),
]

_ALLOWED_AST = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.Constant, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.FloorDiv,
    ast.LShift, ast.RShift, ast.BitAnd, ast.BitOr, ast.BitXor,
    ast.USub, ast.UAdd, ast.Invert, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


def int_eval(expr, env=None):
    """Evaluate a pure integer/bool C++ expression in Python.  Raises EvalError
    if the expression references anything beyond literals and integer env vars.
    Used as the O(N) fast path for CustomListItems <Exec>/<Loop> conditions."""
    src = expr
    for pat, repl in _PY_XLATE:
        src = pat.sub(repl, src)
    try:
        # strip: '!x' -> ' not x' and a leading space is a SyntaxError
        tree = ast.parse(src.strip(), mode="eval")
    except SyntaxError:
        raise EvalError("not a pure int expression: %r" % expr)
    names = {}
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST):
            raise EvalError("unsupported construct in %r" % expr)
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, bool)):
                raise EvalError("non-int literal in %r" % expr)
        if isinstance(node, ast.Name):
            if env is None or node.id not in env:
                raise EvalError("unknown name %r in %r" % (node.id, expr))
            val = env[node.id]
            if not isinstance(val, (int, bool)):
                raise EvalError("name %r is not an int" % node.id)
            names[node.id] = int(val)
    try:
        result = eval(compile(tree, "<natvis>", "eval"), {"__builtins__": {}}, names)
    except ZeroDivisionError:
        raise EvalError("division by zero in %r" % expr)
    if isinstance(result, float):  # C++ '/' on ints truncates toward zero
        result = int(result)
    if isinstance(result, bool):
        return result
    if not isinstance(result, int):
        raise EvalError("non-int result for %r" % expr)
    return result


# -------------------------------------------------- Tier 1: expression engine
#
# Tier 2 (SBValue.EvaluateExpression) costs ~5 SECONDS for the first call in a
# large program -- LLDB has to spin up its expression parser over every module.
# So the fast path is not an optimization here, it is the difference between a
# usable debugger and an unusable one: it must cover essentially every natvis
# expression, not just member chains.  Anything it declines falls back to the
# compiler and the user pays that stall once per process.

_TOKEN_RE = re.compile(r"""
    \s*(
        ->|<<|>>|<=|>=|==|!=|&&|\|\| |
        0[xX][0-9a-fA-F]+[uUlL]*|
        (?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fFuUlL]*|
        '(?:\\.|[^'])'|
        (?:::)?[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*|
        [-+*/%&|^~!<>?:.\[\]()]
    )""", re.VERBOSE)

_UNSIGNED_BASIC = frozenset([
    lldb.eBasicTypeUnsignedChar, lldb.eBasicTypeUnsignedShort,
    lldb.eBasicTypeUnsignedInt, lldb.eBasicTypeUnsignedLong,
    lldb.eBasicTypeUnsignedLongLong, lldb.eBasicTypeBool,
    lldb.eBasicTypeChar16, lldb.eBasicTypeChar32,
])
_FLOAT_BASIC = frozenset([
    lldb.eBasicTypeFloat, lldb.eBasicTypeDouble, lldb.eBasicTypeLongDouble,
])

_ESCAPES = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, "'": 39, '"': 34}


def _tokenize(expr):
    tokens = []
    pos = 0
    n = len(expr)
    while pos < n:
        if expr[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            return None
        tokens.append(m.group(1))
        pos = m.end()
    return tokens


def _parse_literal(tok):
    """Token -> Python number, or None if it isn't a literal."""
    if tok[0] == "'":
        body = tok[1:-1]
        if body.startswith("\\"):
            return _ESCAPES.get(body[1:2], 0)
        return ord(body) if body else 0
    if not (tok[0].isdigit() or tok[0] == "."):
        return None
    if tok[:2] in ("0x", "0X"):
        # strip only integer suffixes: 'f'/'F' are hex digits (0xff, 0xbeef)
        try:
            return int(tok.rstrip("uUlL"), 16)
        except ValueError:
            return None
    body = tok.rstrip("uUlLfF")
    try:
        if "." in body or "e" in body or "E" in body:
            return float(body)
        return int(body, 8) if len(body) > 1 and body[0] == "0" else int(body)
    except ValueError:
        return None


def _num(value):
    """SBValue or Python scalar -> Python number (C value semantics)."""
    if isinstance(value, (int, float)):
        return value
    if not _valid(value):
        raise EvalError("invalid value in arithmetic")
    t = value.GetType()
    if t.IsPointerType() or t.IsArrayType():
        return value.GetValueAsUnsigned(0)
    basic = t.GetCanonicalType().GetBasicType()
    if basic in _FLOAT_BASIC:
        text = value.GetValue()
        try:
            return float(text)
        except (TypeError, ValueError):
            raise EvalError("cannot read float value")
    if basic in _UNSIGNED_BASIC:
        return value.GetValueAsUnsigned(0)
    return value.GetValueAsSigned(0)


def _cdiv(a, b):
    if b == 0:
        raise EvalError("division by zero")
    if isinstance(a, float) or isinstance(b, float):
        return a / b
    q = abs(a) // abs(b)                      # C truncates toward zero,
    return -q if (a < 0) != (b < 0) else q    # Python // floors


def _cmod(a, b):
    if b == 0:
        raise EvalError("division by zero")
    if isinstance(a, float) or isinstance(b, float):
        return math.fmod(a, b)
    return a - _cdiv(a, b) * b


def _valid(sbv):
    return sbv is not None and sbv.IsValid() and sbv.GetError().Success()


def raw_value(sbv):
    """Strip reference-ness and any synthetic child filter.

    Critical: once our own synthetic provider is registered for a type,
    Dereference()/children return the *visualized* view, whose children are
    natvis items ([size], [0], ...) rather than the real fields.  Expression
    evaluation must always see the real fields, or every member lookup on a
    visualized type silently falls through to the compiler."""
    if not _valid(sbv):
        return sbv
    out = sbv.GetNonSyntheticValue()
    t = out.GetType()
    if t.IsValid() and t.IsReferenceType():
        deref = out.Dereference()
        if _valid(deref):
            out = deref.GetNonSyntheticValue()
    return out


def _child_member(cur, name):
    cur = raw_value(cur)
    child = cur.GetChildMemberWithName(name)
    if not _valid(child) and cur.TypeIsPointerType():
        deref = cur.Dereference()
        if _valid(deref):
            child = deref.GetNonSyntheticValue().GetChildMemberWithName(name)
    return child


def _index_child(cur, i):
    t = cur.GetType()
    if t.IsArrayType():
        child = cur.GetChildAtIndex(i)
        if _valid(child):
            return child
    if t.IsPointerType():
        child = cur.GetChildAtIndex(i, lldb.eNoDynamicValues, True)
        if _valid(child):
            return child
        elem = t.GetPointeeType()
        base = cur.GetValueAsUnsigned(0)
        if base and elem.IsValid() and elem.GetByteSize() > 0:
            return cur.CreateValueFromAddress(
                "[%d]" % i, base + i * elem.GetByteSize(), elem)
    else:
        child = cur.GetChildAtIndex(i)
        if _valid(child):
            return child
    return None


class _Decline(Exception):
    """This expression needs the real compiler (Tier 2)."""


class _Evaluator:
    """Recursive-descent evaluator with C precedence and value semantics.

    Operands are SBValues (member chains) or Python numbers (literals and
    computed results); `_num` bridges the two.  Anything outside the grammar --
    casts, calls, sizeof -- raises _Decline so the caller falls back to Tier 2.
    """

    def __init__(self, tokens, root, env):
        self.tokens = tokens
        self.pos = 0
        self.root = root
        self.env = env or {}

    # ------------------------------------------------------------- helpers

    def peek(self, ahead=0):
        i = self.pos + ahead
        return self.tokens[i] if i < len(self.tokens) else None

    def take(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def expect(self, tok):
        if self.take() != tok:
            raise _Decline("expected %r" % tok)

    # --------------------------------------------- precedence climbing

    def parse(self):
        return self.ternary()

    def ternary(self):
        cond = self.binary(0)
        if self.peek() != "?":
            return cond
        self.take()
        then = self.ternary()
        self.expect(":")
        other = self.ternary()
        return then if _truthy_operand(cond) else other

    # binary operator table, lowest precedence first
    _LEVELS = [
        ("||",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="),
        ("<", ">", "<=", ">="), ("<<", ">>"), ("+", "-"), ("*", "/", "%"),
    ]

    def binary(self, level):
        if level >= len(self._LEVELS):
            return self.unary()
        ops = self._LEVELS[level]
        left = self.binary(level + 1)
        while True:
            tok = self.peek()
            if tok not in ops:
                return left
            self.take()
            # short-circuit, so `p != 0 && p->x` never derefs a null p
            if tok == "&&":
                if not _truthy_operand(left):
                    self._skip_operand(level + 1)
                    left = 0
                    continue
                left = 1 if _truthy_operand(self.binary(level + 1)) else 0
                continue
            if tok == "||":
                if _truthy_operand(left):
                    self._skip_operand(level + 1)
                    left = 1
                    continue
                left = 1 if _truthy_operand(self.binary(level + 1)) else 0
                continue
            right = self.binary(level + 1)
            left = self._apply(tok, left, right)

    def _skip_operand(self, level):
        """Consume (without evaluating) the operand of a short-circuited op."""
        depth = 0
        while True:
            tok = self.peek()
            if tok is None:
                return
            if tok in "([":
                depth += 1
            elif tok in ")]":
                if depth == 0:
                    return
                depth -= 1
            elif depth == 0 and tok in (":", "?"):
                return
            elif depth == 0:
                for lvl in range(0, level):
                    if tok in self._LEVELS[lvl]:
                        return
            self.take()

    def _apply(self, op, left, right):
        if op == "+" or op == "-":
            ptr = _pointer_arith(left, right, op)
            if ptr is not None:
                return ptr
        a, b = _num(left), _num(right)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return _cdiv(a, b)
        if op == "%":
            return _cmod(a, b)
        if op == "==":
            return 1 if a == b else 0
        if op == "!=":
            return 1 if a != b else 0
        if op == "<":
            return 1 if a < b else 0
        if op == ">":
            return 1 if a > b else 0
        if op == "<=":
            return 1 if a <= b else 0
        if op == ">=":
            return 1 if a >= b else 0
        ia, ib = int(a), int(b)
        if op == "&":
            return ia & ib
        if op == "|":
            return ia | ib
        if op == "^":
            return ia ^ ib
        if op == "<<":
            return ia << ib
        if op == ">>":
            return ia >> ib
        raise _Decline("unknown operator %r" % op)

    def unary(self):
        tok = self.peek()
        if tok == "*":
            self.take()
            inner = self.unary()
            if isinstance(inner, (int, float)):
                raise _Decline("cannot dereference a number")
            out = raw_value(inner.Dereference())
            if not _valid(out):
                raise EvalError("cannot dereference")
            return out
        if tok == "&":
            self.take()
            inner = self.unary()
            if isinstance(inner, (int, float)):
                raise _Decline("cannot take address of a number")
            out = inner.AddressOf()
            if not _valid(out):
                raise EvalError("cannot take address")
            return out
        if tok == "!":
            self.take()
            return 0 if _truthy_operand(self.unary()) else 1
        if tok == "-":
            self.take()
            return -_num(self.unary())
        if tok == "+":
            self.take()
            return _num(self.unary())
        if tok == "~":
            self.take()
            return ~int(_num(self.unary()))
        return self.postfix()

    def postfix(self):
        cur = self.primary()
        while True:
            tok = self.peek()
            if tok in (".", "->"):
                self.take()
                name = self.take()
                if name is None or not name[:1].isalpha() and name[:1] != "_":
                    raise _Decline("bad member access")
                if isinstance(cur, (int, float)):
                    raise _Decline("member access on a number")
                nxt = _child_member(cur, name)
                if not _valid(nxt):
                    raise EvalError("no member %r on %s"
                                    % (name, cur.GetTypeName()))
                cur = nxt
            elif tok == "[":
                self.take()
                idx = int(_num(self.ternary()))
                self.expect("]")
                if isinstance(cur, (int, float)):
                    raise _Decline("indexing a number")
                nxt = _index_child(cur, idx)
                if nxt is None or not _valid(nxt):
                    raise EvalError("cannot index %s" % cur.GetTypeName())
                cur = nxt
            elif tok == "(":
                raise _Decline("function call")
            else:
                return cur

    _TYPE_NOISE = frozenset(["const", "volatile", "struct", "class", "union",
                             "enum", "unsigned", "signed"])

    def primary(self):
        tok = self.take()
        if tok is None:
            raise _Decline("unexpected end of expression")
        if tok == "(":
            cast = self._try_cast()
            if cast is not None:
                return cast
            inner = self.ternary()
            self.expect(")")
            return inner
        lit = _parse_literal(tok)
        if lit is not None:
            return lit
        if not (tok[:1].isalpha() or tok[:1] == "_" or tok[:2] == "::"):
            raise _Decline("unexpected token %r" % tok)
        if tok == "true":
            return 1
        if tok in ("false", "nullptr", "NULL"):
            return 0
        if tok == "this":
            return self.root
        if tok in self.env:
            return self.env[tok]
        root = raw_value(self.root)
        if not _valid(root):
            raise EvalError("no value context for %r" % tok)
        child = root.GetChildMemberWithName(tok)
        if _valid(child):
            return child
        raise EvalError("no member %r on %s"
                        % (tok, self.root.GetTypeName()))


    def _try_cast(self):
        """Just consumed '('.  If this is a C-style cast to a known type,
        apply it and return the value; otherwise rewind and return None.

        `*(Base*)this` is the standard natvis idiom for exposing base-class
        members, so handling it here keeps a very common pattern off Tier 2.
        The type lookup itself is what disambiguates a cast from a
        parenthesized expression -- `(a)*b` only casts if `a` names a type."""
        start = self.pos
        words = []
        stars = 0
        while True:
            tok = self.peek()
            if tok is None:
                break
            if tok == "*":
                stars += 1
                self.take()
                continue
            if tok == ")":
                break
            if stars or not (tok[:1].isalpha() or tok[:1] == "_"):
                break
            words.append(tok)
            self.take()
        if not words or self.peek() != ")":
            self.pos = start
            return None
        name = " ".join(w for w in words if w not in self._TYPE_NOISE)
        if name.startswith("::"):
            name = name[2:]          # global-scope qualifier, e.g. ::RID
        if not name:
            self.pos = start
            return None
        target = self.root.GetTarget()
        sbtype = target.FindFirstType(name)
        if not sbtype.IsValid():
            self.pos = start
            return None
        self.take()                       # the ')'
        for _ in range(stars):
            sbtype = sbtype.GetPointerType()
        operand = self.unary()
        if isinstance(operand, (int, float)):
            self.pos = start
            raise _Decline("cast of a computed number")
        if stars and not operand.GetType().IsPointerType():
            operand = operand.AddressOf()   # natvis `this` is the object here
            if not _valid(operand):
                raise EvalError("cannot take address for cast")
        out = operand.Cast(sbtype)
        if not _valid(out):
            raise EvalError("cast to %s failed" % sbtype.GetName())
        return out


def _truthy_operand(value):
    if isinstance(value, (int, float)):
        return value != 0
    return truthy(value)


def _pointer_arith(left, right, op):
    """`ptr + n` / `ptr - n` keeping the pointer type, else None."""
    if isinstance(left, (int, float)) or not _valid(left):
        return None
    t = left.GetType()
    if not t.IsPointerType():
        return None
    if not isinstance(right, (int, float)):
        if not _valid(right) or right.GetType().IsPointerType():
            return None      # ptr - ptr is a plain count; let _apply do it
    elem = t.GetPointeeType()
    step = elem.GetByteSize() if elem.IsValid() else 0
    if not step:
        return None
    n = int(_num(right))
    addr = left.GetValueAsUnsigned(0) + (n * step if op == "+" else -n * step)
    target = left.GetTarget()
    data = lldb.SBData.CreateDataFromUInt64Array(
        target.GetByteOrder(), target.GetAddressByteSize(), [addr])
    out = target.CreateValueFromData(left.GetName() or "ptr", data, t)
    return out if _valid(out) else None


_decline = {"why": ""}


def make_value(root, name, number):
    """Wrap a computed Python number in an SBValue, so a natvis <Item> whose
    expression is arithmetic still yields a real child (and never needs the
    compiler just to materialize a number)."""
    target = root.GetTarget()
    if isinstance(number, float):
        data = lldb.SBData.CreateDataFromDoubleArray(
            target.GetByteOrder(), target.GetAddressByteSize(), [number])
        vtype = target.GetBasicType(lldb.eBasicTypeDouble)
    else:
        data = lldb.SBData.CreateDataFromSInt64Array(
            target.GetByteOrder(), target.GetAddressByteSize(), [int(number)])
        vtype = target.GetBasicType(lldb.eBasicTypeLongLong)
    out = target.CreateValueFromData(name, data, vtype)
    if not _valid(out):
        raise EvalError("cannot materialize computed value %r" % number)
    return out


def _run_evaluator(root, expr, env):
    """Returns (value, handled).  handled=False means the fast path could not
    produce an answer -- either the grammar declined or resolution failed --
    and the caller must fall back to the compiler, which understands things we
    do not (casts, base-class names, statics, operators)."""
    tokens = _tokenize(expr)
    if not tokens:
        _decline["why"] = "cannot tokenize"
        return None, False
    ev = _Evaluator(tokens, root, env)
    try:
        result = ev.parse()
    except (_Decline, EvalError) as exc:
        _decline["why"] = str(exc)
        return None, False
    if ev.pos != len(tokens):
        _decline["why"] = "trailing tokens %r" % (tokens[ev.pos:],)
        return None, False
    if not isinstance(result, (int, float)) and not _valid(result):
        _decline["why"] = "invalid result value"
        return None, False
    return result, True


def fast_eval(root, expr, env=None):
    """Tier 1.  Returns an SBValue, or None when the expression is outside the
    fast grammar (caller falls back to Tier 2).  Numeric results are handled by
    eval_scalar; this entry point exists for callers that need a real SBValue."""
    result, ok = _run_evaluator(root, expr, env)
    if not ok or isinstance(result, (int, float)):
        return None
    return result if _valid(result) else None


def eval_scalar(root, expr, env=None):
    """Tier 1 for numeric/boolean results.  Returns a Python number, or raises
    EvalError (evaluation genuinely failed) / _Decline-as-None via NotScalar."""
    result, ok = _run_evaluator(root, expr, env)
    if not ok:
        raise NotScalar(expr)
    if isinstance(result, (int, float)):
        return result
    return _num(result)


class NotScalar(EvalError):
    """The fast evaluator declined; the caller should try Tier 2."""


# ----------------------------------------------------------- Tier 2: JIT eval

def _env_repl_text(name, val):
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, int):
        return str(val)
    t = val.GetType()
    if t.IsPointerType():
        return "((%s)0x%x)" % (t.GetName(), val.GetValueAsUnsigned(0))
    if t.GetBasicType() != lldb.eBasicTypeInvalid and \
            val.GetValue() is not None:
        return "((%s)%s)" % (t.GetName(), val.GetValue())
    addr = val.GetLoadAddress()
    if addr == lldb.LLDB_INVALID_ADDRESS:
        raise EvalError("cannot substitute env var %r into expression" % name)
    return "(*(%s*)0x%x)" % (t.GetName(), addr)


def _env_substitute(expr, env):
    # single pass over one alternation: replacement text must never be
    # rescanned by another env var's pattern (e.g. a var named like a type)
    if not env:
        return expr
    names = sorted(env, key=len, reverse=True)
    pattern = re.compile(
        r"\b(?:%s)\b" % "|".join(re.escape(n) for n in names))
    return pattern.sub(lambda m: _env_repl_text(m.group(0), env[m.group(0)]),
                       expr)


_counters = {"fast": 0, "slow": 0, "slow_fail": 0, "int": 0}


def counters():
    return dict(_counters)


def slow_eval(valobj, expr, env=None):
    # Tier 2 is expensive -- the first call in a big program can take seconds
    # while LLDB builds its expression parser. Log every one so `natvis verbose
    # on` shows exactly which expressions are costing the user time.
    if log.isEnabledFor(10):
        import traceback
        caller = "?"
        for fr in reversed(traceback.extract_stack()[:-1]):
            if "natvis/" in fr.filename and "expr.py" not in fr.filename:
                caller = "%s:%s" % (fr.filename.rsplit("/", 1)[-1], fr.name)
                break
        log.debug("Tier 2 eval: %r on %s from %s (fast path: %s)", expr,
                  valobj.GetTypeName(), caller, _decline["why"])
    expr = _env_substitute(expr, env)
    opts = lldb.SBExpressionOptions()
    opts.SetIgnoreBreakpoints(True)
    opts.SetTimeoutInMicroSeconds(500000)
    result = valobj.EvaluateExpression(expr, opts)
    if not _valid(result):
        _counters["slow_fail"] += 1
        msg = "?"
        if result is not None and result.GetError().Fail():
            msg = result.GetError().GetCString() or "?"
        raise EvalError("cannot evaluate %r: %s" % (expr, msg))
    _counters["slow"] += 1
    return result


def evaluate_any(valobj, expr, env=None):
    """Evaluate and return (value, is_sbvalue).  `value` is an SBValue when the
    expression designates an object, or a Python number when it computes one.
    Callers that must have an SBValue use evaluate() instead."""
    expr = expr.strip()
    if not expr:
        raise EvalError("empty expression")
    try:
        result, ok = _run_evaluator(valobj, expr, env)
    except EvalError:
        result, ok = None, False
    if ok:
        _counters["fast"] += 1
        if isinstance(result, (int, float)):
            return result, False
        if _valid(result):
            return result, True
    return slow_eval(valobj, expr, env), True


def evaluate(valobj, expr, env=None):
    """Evaluate a (preprocessed) natvis expression in the context of valobj.
    Returns an SBValue; raises EvalError."""
    expr = expr.strip()
    if not expr:
        raise EvalError("empty expression")
    result = fast_eval(valobj, expr, env)
    if result is not None:
        _counters["fast"] += 1
        return result
    return slow_eval(valobj, expr, env)


def truthy(sbv):
    v = sbv.GetValue()
    if v == "true":
        return True
    if v == "false":
        return False
    t = sbv.GetType()
    if t.IsPointerType():
        return sbv.GetValueAsUnsigned(0) != 0
    basic = t.GetCanonicalType().GetBasicType()
    if basic in (lldb.eBasicTypeFloat, lldb.eBasicTypeDouble,
                 lldb.eBasicTypeLongDouble):
        try:
            return float(v) != 0.0
        except (TypeError, ValueError):
            return False
    return sbv.GetValueAsUnsigned(0) != 0


def evaluate_bool(valobj, expr, env=None):
    try:
        result, ok = _run_evaluator(valobj, expr.strip(), env)
        if ok:
            _counters["int"] += 1
            return _truthy_operand(result)
    except EvalError:
        pass
    return truthy(evaluate(valobj, expr, env))


def evaluate_int(valobj, expr, env=None, signed=False):
    try:
        result, ok = _run_evaluator(valobj, expr.strip(), env)
        if ok:
            _counters["int"] += 1
            if isinstance(result, (int, float)):
                return int(result)
            return (result.GetValueAsSigned(0) if signed
                    else result.GetValueAsUnsigned(0))
    except EvalError:
        pass
    sbv = evaluate(valobj, expr, env)
    return sbv.GetValueAsSigned(0) if signed else sbv.GetValueAsUnsigned(0)
