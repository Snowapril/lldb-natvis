"""DisplayString interpolation and the LLDB summary entry point."""

import threading
from functools import lru_cache

import lldb

from . import log
from .expr import EvalError, evaluate, evaluate_bool, evaluate_int, preprocess
from .formatspec import parse_spec, render_value

_MAX_DEPTH = 8
_ERR_MARK = "??"
_ARRAY_PREVIEW_CAP = 32

_tls = threading.local()


def _state():
    if not hasattr(_tls, "depth"):
        _tls.depth = 0
        _tls.active = set()
    return _tls


# ------------------------------------------------------------ brace parsing

@lru_cache(maxsize=4096)
def parse_display_text(text):
    """Split a DisplayString body into parts:
    ('lit', str) | ('expr', expression, FormatSpec-or-None)."""
    parts = []
    lit = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            if i + 1 < n and text[i + 1] == "{":
                lit.append("{")
                i += 2
                continue
            end = _find_close_brace(text, i)
            if end < 0:
                lit.append(text[i:])
                break
            if lit:
                parts.append(("lit", "".join(lit)))
                lit = []
            expr, spec = _split_expr_spec(text[i + 1:end])
            parts.append(("expr", expr, spec))
            i = end + 1
        elif ch == "}":
            if i + 1 < n and text[i + 1] == "}":
                lit.append("}")
                i += 2
            else:
                lit.append("}")
                i += 1
        else:
            lit.append(ch)
            i += 1
    if lit:
        parts.append(("lit", "".join(lit)))
    return tuple(parts)


def _find_close_brace(text, open_idx):
    depth = 0
    quote = None
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_expr_spec(body):
    """Split '{expr,spec}' innards at the last top-level comma whose tail parses
    as a valid format specifier; otherwise the comma belongs to the expr."""
    depth = 0
    quote = None
    last_comma = -1
    for i, ch in enumerate(body):
        if quote:
            if ch == quote and body[i - 1] != "\\":
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "([<{":
            depth += 1
        elif ch in ")]>}":
            if ch == ">" and i > 0 and body[i - 1] == "-":
                continue
            depth -= 1
        elif ch == "," and depth == 0:
            last_comma = i
    if last_comma >= 0:
        spec = parse_spec(body[last_comma + 1:])
        if spec is not None:
            return body[:last_comma].strip(), spec
    return body.strip(), None


# ------------------------------------------------------------- interpolation

def interpolate(text, valobj, bindings=(), intrinsics=None, env=None, view="",
                raise_errors=False):
    """Render a DisplayString body.  With raise_errors=True an EvalError
    propagates (Optional DisplayString semantics: skip the whole alternative);
    otherwise the failed {expr} renders as an inline error marker."""
    out = []
    for part in parse_display_text(text):
        if part[0] == "lit":
            out.append(part[1])
            continue
        _tag, raw_expr, spec = part
        try:
            expr = preprocess(raw_expr, bindings, intrinsics)
            if spec is not None and spec.kind == "array":
                out.append(_render_array(valobj, expr, spec, bindings,
                                         intrinsics, env, view))
                continue
            sbv = evaluate(valobj, expr, env)
            out.append(render_value(sbv, spec, _child_summary, view) or "")
        except EvalError as exc:
            if raise_errors:
                raise
            log.debug("interpolate {%s}: %s", raw_expr, exc)
            out.append(_ERR_MARK)
    return "".join(out)


def _render_array(valobj, expr, spec, bindings, intrinsics, env, view):
    size_expr = preprocess(spec.array_expr, bindings, intrinsics)
    count = evaluate_int(valobj, size_expr, env)
    base = evaluate(valobj, expr, env)
    t = base.GetType()
    elems = []
    shown = min(count, _ARRAY_PREVIEW_CAP)
    for i in range(shown):
        if t.IsPointerType():
            child = base.GetChildAtIndex(i, lldb.eNoDynamicValues, True)
        else:
            child = base.GetChildAtIndex(i)
        if not child.IsValid():
            break
        elems.append(render_value(child, None, _child_summary, view) or "?")
    suffix = ", ..." if count > shown else ""
    return "{%s%s}" % (", ".join(elems), suffix)


# ------------------------------------------------------------ summary entry

def _child_summary(sbv, view):
    """Summary text for a nested value: prefer our natvis machinery (so views
    propagate), fall back to lldb's own formatters."""
    state = _state()
    if state.depth >= _MAX_DEPTH:
        return "{...}"
    from . import registry
    resolved = registry.REGISTRY.resolve_for_value(sbv)
    if resolved is not None:
        return _summary_for(resolved, view)
    if view:
        return None
    summary = sbv.GetSummary()
    return summary or None


def summary_key(val):
    return (val.GetLoadAddress(), val.GetName() or "", val.GetTypeName() or "")


def _summary_for(resolved, view):
    """resolved = registry.Resolved(...); runs the DisplayString alternatives."""
    state = _state()
    key = summary_key(resolved.val)
    if key in state.active:
        return "{...}"
    state.active.add(key)
    state.depth += 1
    try:
        for ds in resolved.display_strings():
            if not ds.allows(view):
                continue
            if ds.condition is not None:
                try:
                    cond = preprocess(ds.condition, resolved.bindings,
                                      resolved.intrinsics())
                    if not evaluate_bool(resolved.val, cond):
                        continue
                except EvalError:
                    continue
            try:
                return interpolate(ds.text, resolved.val, resolved.bindings,
                                   resolved.intrinsics(), None, view,
                                   raise_errors=ds.optional)
            except EvalError:
                if ds.optional:
                    continue
                raise
        for sv in resolved.string_views():
            if not sv.allows(view):
                continue
            try:
                if sv.condition is not None:
                    cond = preprocess(sv.condition, resolved.bindings,
                                      resolved.intrinsics())
                    if not evaluate_bool(resolved.val, cond):
                        continue
                expr_text, spec = _split_expr_spec(sv.expr)
                expr = preprocess(expr_text, resolved.bindings,
                                  resolved.intrinsics())
                sbv = evaluate(resolved.val, expr)
                return render_value(sbv, spec, _child_summary, view)
            except EvalError:
                continue
        return None
    finally:
        state.depth -= 1
        state.active.discard(key)


def natvis_summary(valobj, internal_dict):
    """LLDB type summary entry point (registered via CreateWithFunctionName)."""
    try:
        from . import registry
        val = valobj.GetNonSyntheticValue()
        t = val.GetType()
        if t.IsValid() and t.IsReferenceType():
            deref = val.Dereference()
            if deref.IsValid():
                val = deref
        resolved = registry.REGISTRY.resolve_for_value(val)
        if resolved is None:
            return ""
        return _summary_for(resolved, "") or ""
    except EvalError as exc:
        log.debug("summary failed for %s: %s", valobj.GetTypeName(), exc)
        return _ERR_MARK
    except Exception as exc:                       # never break the debugger
        log.warning("summary error for %s: %s", valobj.GetTypeName(), exc)
        return ""


def stringview_text(valobj):
    """Full StringView content for the `natvis stringview` command."""
    from . import registry
    val = valobj.GetNonSyntheticValue()
    resolved = registry.REGISTRY.resolve_for_value(val)
    if resolved is None:
        return None
    for sv in resolved.string_views():
        try:
            if sv.condition is not None:
                cond = preprocess(sv.condition, resolved.bindings,
                                  resolved.intrinsics())
                if not evaluate_bool(resolved.val, cond):
                    continue
            expr_text, spec = _split_expr_spec(sv.expr)
            expr = preprocess(expr_text, resolved.bindings,
                              resolved.intrinsics())
            sbv = evaluate(resolved.val, expr)
            return render_value(sbv, spec, _child_summary, "")
        except EvalError:
            continue
    return None
