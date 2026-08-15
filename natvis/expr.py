"""Natvis expression preprocessing and two-tier evaluation.

Tier 1 walks SBValue children directly (no compiler) for the member/deref/index
chains that dominate real natvis files.  Tier 2 falls back to
SBValue.EvaluateExpression, which evaluates C++ with the object's members in
scope -- exactly the natvis semantic."""

import ast
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


# ----------------------------------------------------------- Tier 1: chains

_TOKEN_RE = re.compile(r"""
    \s*(
        ->|\.|\[|\]|\(|\)|\*|&|
        0[xX][0-9a-fA-F]+|\d+|
        [A-Za-z_][A-Za-z0-9_]*(::[A-Za-z_][A-Za-z0-9_]*)*
    )""", re.VERBOSE)


def _tokenize_chain(expr):
    tokens = []
    pos = 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            if expr[pos:].strip() == "":
                break
            return None
        tokens.append(m.group(1))
        pos = m.end()
    return tokens


def _valid(sbv):
    return sbv is not None and sbv.IsValid() and sbv.GetError().Success()


def _child_member(cur, name):
    child = cur.GetChildMemberWithName(name)
    if not _valid(child) and cur.TypeIsPointerType():
        deref = cur.Dereference()
        if _valid(deref):
            child = deref.GetChildMemberWithName(name)
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


class _ChainParser:
    """Recursive-descent parser/evaluator for the restricted grammar:
       unary  := ('*'|'&')* chain
       chain  := primary ( '.' IDENT | '->' IDENT | '[' index ']' )*
       primary:= IDENT | 'this' | '(' unary ')'
    """

    def __init__(self, tokens, root, env):
        self.tokens = tokens
        self.pos = 0
        self.root = root
        self.env = env

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def parse_unary(self):
        tok = self.peek()
        if tok == "*":
            self.next()
            inner = self.parse_unary()
            if inner is None:
                return None
            out = inner.Dereference()
            return out if _valid(out) else None
        if tok == "&":
            self.next()
            inner = self.parse_unary()
            if inner is None:
                return None
            out = inner.AddressOf()
            return out if _valid(out) else None
        return self.parse_chain()

    def parse_chain(self):
        cur = self.parse_primary()
        if cur is None:
            return None
        while True:
            tok = self.peek()
            if tok in (".", "->"):
                self.next()
                name = self.next()
                if name is None or not re.match(r"^[A-Za-z_]", name):
                    return None
                cur = _child_member(cur, name)
                if not _valid(cur):
                    return None
            elif tok == "[":
                self.next()
                idx = self.parse_index()
                if idx is None or self.next() != "]":
                    return None
                cur = _index_child(cur, idx)
                if cur is None or not _valid(cur):
                    return None
            else:
                break
        return cur

    def parse_primary(self):
        tok = self.next()
        if tok is None:
            return None
        if tok == "(":
            inner = self.parse_unary()
            if inner is None or self.next() != ")":
                return None
            return inner
        if tok == "this":
            return self.root
        if re.match(r"^[A-Za-z_]", tok):
            if self.env and tok in self.env:
                val = self.env[tok]
                if isinstance(val, int):
                    return None       # int env var as value head -> slow path
                return val
            child = self.root.GetChildMemberWithName(tok)
            if _valid(child):
                return child
            return None
        return None

    def parse_index(self):
        tok = self.peek()
        if tok is None:
            return None
        if re.match(r"^\d|^0[xX]", tok):
            self.next()
            try:
                return int(tok, 0)
            except ValueError:
                return None
        if self.env and tok in self.env and isinstance(self.env[tok], int):
            self.next()
            return self.env[tok]
        # sub-chain index (e.g. arr[mSize - 1] is NOT handled here -> slow)
        saved = self.pos
        sub = self.parse_chain()
        if sub is not None and _valid(sub):
            return sub.GetValueAsSigned(0)
        self.pos = saved
        return None


def fast_eval(root, expr, env=None):
    """Tier 1.  Returns an SBValue, or None when the expression is outside the
    fast grammar / resolution failed (caller falls back to Tier 2)."""
    tokens = _tokenize_chain(expr)
    if not tokens:
        return None
    parser = _ChainParser(tokens, root, env)
    result = parser.parse_unary()
    if result is None or parser.pos != len(tokens):
        return None
    return result if _valid(result) else None


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


_ENV_NULLCHECK_RE = re.compile(
    r"^\s*(!?)\s*([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?:(==|!=)\s*(0|nullptr|NULL)\s*)?$")


def evaluate_bool(valobj, expr, env=None):
    try:
        result = int_eval(expr, env)
        _counters["int"] += 1
        return bool(result)
    except EvalError:
        pass
    if env:
        m = _ENV_NULLCHECK_RE.match(expr)
        if m and m.group(2) in env and not isinstance(env[m.group(2)], int):
            result = truthy(env[m.group(2)])
            if m.group(1) == "!" or m.group(3) == "==":
                result = not result
            _counters["int"] += 1
            return result
    return truthy(evaluate(valobj, expr, env))


def evaluate_int(valobj, expr, env=None, signed=False):
    try:
        result = int_eval(expr, env)
        _counters["int"] += 1
        return int(result)
    except EvalError:
        pass
    sbv = evaluate(valobj, expr, env)
    return sbv.GetValueAsSigned(0) if signed else sbv.GetValueAsUnsigned(0)
