"""Natvis format specifiers: parse the ',spec' tail of {expr,spec} and render
an SBValue accordingly (mirrors Visual Studio's format specifier table)."""

import re

import lldb

from .expr import EvalError

_SPEC_RE = re.compile(r"^(\d*)([a-zA-Z!]+[0-9]*[a-z]*)$")

_SIMPLE_FORMATS = {
    "d": lldb.eFormatDecimal,
    "o": lldb.eFormatOctal,
    "b": lldb.eFormatBinary,
    "bb": lldb.eFormatBinary,
    "c": lldb.eFormatChar,
    "h": lldb.eFormatHex,
    "hr": lldb.eFormatHex,
    "x": lldb.eFormatHex,
    "xb": lldb.eFormatHex,
}

_NOOPS = {"!", "nr", "nvo", "expand", "hv", "handle"}


class FormatSpec:
    __slots__ = ("kind", "width", "view_name", "array_expr", "raw")

    def __init__(self, kind=None, width=0, view_name=None, array_expr=None,
                 raw=""):
        self.kind = kind
        self.width = width
        self.view_name = view_name
        self.array_expr = array_expr
        self.raw = raw


def parse_spec(text):
    """Parse a format specifier tail (without the leading comma).
    Returns FormatSpec or None if it doesn't look like a valid specifier
    (in which case the comma belonged to the expression)."""
    text = text.strip()
    if not text:
        return None
    if text.startswith("[") and text.endswith("]"):
        return FormatSpec(kind="array", array_expr=text[1:-1], raw=text)
    m = re.match(r"^view\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)$", text)
    if m:
        return FormatSpec(kind="view", view_name=m.group(1), raw=text)
    m = _SPEC_RE.match(text)
    if not m:
        return None
    width = int(m.group(1)) if m.group(1) else 0
    body = m.group(2)
    known = set(_SIMPLE_FORMATS) | _NOOPS | {
        "X", "H", "Xb", "Hb", "s", "sa", "sb", "s8", "s8b", "su", "sub",
        "s32", "s32b", "bstr", "en", "e", "f", "g", "na", "nd",
    }
    if body not in known:
        return None
    return FormatSpec(kind=body, width=width, raw=text)


def _read_string(sbv, char_size, encoding):
    process = sbv.GetProcess()
    if not process or not process.IsValid():
        return None
    t = sbv.GetType()
    if t.IsPointerType():
        addr = sbv.GetValueAsUnsigned(0)
    elif t.IsArrayType():
        addr = sbv.GetLoadAddress()
    else:
        addr = sbv.GetLoadAddress()
    if not addr or addr == lldb.LLDB_INVALID_ADDRESS:
        return None
    error = lldb.SBError()
    if char_size == 1:
        s = process.ReadCStringFromMemory(addr, 4096, error)
        if error.Fail():
            return None
        if encoding == "utf-8":
            try:
                s = s.encode("latin-1", "ignore").decode("utf-8", "replace")
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass
        return s
    # UTF-16 / UTF-32: read chunks until NUL code unit
    raw = b""
    chunk = 256
    total = 0
    while total < 4096:
        data = process.ReadMemory(addr + total, chunk, error)
        if error.Fail() or not data:
            break
        raw += data
        total += len(data)
        nul = b"\x00" * char_size
        for i in range(0, len(raw) - char_size + 1, char_size):
            if raw[i:i + char_size] == nul:
                raw = raw[:i]
                total = 1 << 30
                break
    try:
        return raw.decode(encoding, "replace")
    except (UnicodeDecodeError, LookupError):
        return None


def _with_format(sbv, fmt):
    prior = sbv.GetFormat()
    sbv.SetFormat(fmt)
    out = sbv.GetValue()
    sbv.SetFormat(prior)
    return out


def _enum_render(sbv):
    out = _with_format(sbv, lldb.eFormatEnum)
    val = sbv.GetValueAsUnsigned(0)
    if out is not None and not out.lstrip("-").isdigit():
        return out
    # flags decomposition
    t = sbv.GetType().GetCanonicalType()
    members = t.GetEnumMembers()
    if members and members.IsValid():
        names = []
        remaining = val
        exact = None
        for i in range(members.GetSize()):
            m = members.GetTypeEnumMemberAtIndex(i)
            mval = m.GetValueAsUnsigned()
            if mval == val:
                exact = m.GetName()
            if mval and (remaining & mval) == mval:
                names.append(m.GetName())
                remaining &= ~mval
        if exact is not None:
            return exact
        if names and remaining == 0:
            return " | ".join(names)
    return str(val)


def render_value(sbv, spec, render_child_summary, current_view=""):
    """Render an evaluated SBValue as VS would inside a DisplayString.

    render_child_summary(sbv, view) -> Optional[str] callback lets display.py
    inject natvis-aware nested summaries with its recursion guard."""
    if spec is None:
        return _default_render(sbv, render_child_summary, current_view)
    kind = spec.kind
    if kind in _NOOPS:
        return _default_render(sbv, render_child_summary, current_view)
    if kind in _SIMPLE_FORMATS:
        out = _with_format(sbv, _SIMPLE_FORMATS[kind])
        if out is None:
            out = _default_render(sbv, render_child_summary, current_view)
        if spec.width and out is not None and kind in ("x", "h", "d", "o", "b"):
            prefix = ""
            body = out
            if out.startswith("0x") or out.startswith("0b"):
                prefix, body = out[:2], out[2:]
            out = prefix + body.rjust(spec.width, "0")
        return out
    if kind in ("X", "H", "Xb", "Hb"):
        out = _with_format(sbv, lldb.eFormatHex)
        if out is None:
            return _default_render(sbv, render_child_summary, current_view)
        return "0x" + out[2:].upper() if out.startswith("0x") else out.upper()
    if kind in ("s", "sa", "sb", "s8", "s8b", "bstr"):
        enc = "utf-8" if kind.startswith("s8") else "latin-1"
        s = _read_string(sbv, 1, enc)
        if s is None:
            return _default_render(sbv, render_child_summary, current_view)
        return s if kind in ("sb", "s8b") else '"%s"' % s
    if kind in ("su", "sub"):
        s = _read_string(sbv, 2, "utf-16-le")
        if s is None:
            return _default_render(sbv, render_child_summary, current_view)
        return s if kind == "sub" else '"%s"' % s
    if kind in ("s32", "s32b"):
        s = _read_string(sbv, 4, "utf-32-le")
        if s is None:
            return _default_render(sbv, render_child_summary, current_view)
        return s if kind == "s32b" else '"%s"' % s
    if kind == "en":
        return _enum_render(sbv)
    if kind in ("e", "f", "g"):
        v = sbv.GetValue()
        try:
            f = float(v)
        except (TypeError, ValueError):
            return v if v is not None else "?"
        if kind == "e":
            return "%e" % f
        if kind == "f":
            return "%f" % f
        return "%g" % f
    if kind == "na":
        if sbv.GetType().IsPointerType():
            deref = sbv.Dereference()
            if deref.IsValid() and deref.GetError().Success():
                return _default_render(deref, render_child_summary,
                                       current_view)
        return _default_render(sbv, render_child_summary, current_view)
    if kind == "nd":
        static = sbv.GetStaticValue()
        return _default_render(static if static.IsValid() else sbv,
                               render_child_summary, current_view)
    if kind == "view":
        return _default_render(sbv, render_child_summary, spec.view_name or "")
    if kind == "array":
        raise EvalError("array spec must be handled by caller")  # display.py
    return _default_render(sbv, render_child_summary, current_view)


def fmt_number(value):
    """Render a Python number the way LLDB renders the equivalent value
    (floats that are whole print without a trailing '.0')."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return repr(value)
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        return repr(value)
    return str(value)


def render_number(value, spec):
    """Render a computed (Tier-1) numeric result under a format specifier.
    Specifiers that need a live object (,s ,en ,na ...) just print decimal."""
    if spec is None:
        return fmt_number(value)
    kind = spec.kind
    ival = int(value)
    if kind in ("d",):
        out = str(ival)
    elif kind in ("x", "h", "xb", "hr"):
        out = "0x%x" % (ival & 0xFFFFFFFFFFFFFFFF if ival < 0 else ival)
    elif kind in ("X", "H", "Xb", "Hb"):
        out = "0x%X" % (ival & 0xFFFFFFFFFFFFFFFF if ival < 0 else ival)
    elif kind == "o":
        out = "0%o" % ival
    elif kind in ("b", "bb"):
        out = "0b{:b}".format(ival)
    elif kind == "c":
        out = chr(ival & 0xFF) if 0 <= (ival & 0xFF) < 0x110000 else str(ival)
    elif kind == "e":
        out = "%e" % value
    elif kind == "f":
        out = "%f" % value
    elif kind == "g":
        out = "%g" % value
    else:
        return fmt_number(value)
    if spec.width and kind in ("d", "x", "h", "X", "H", "o", "b"):
        prefix, body = ("", out)
        if out[:2] in ("0x", "0b"):
            prefix, body = out[:2], out[2:]
        out = prefix + body.rjust(spec.width, "0")
    return out


def _default_render(sbv, render_child_summary, current_view=""):
    if render_child_summary is not None:
        summary = render_child_summary(sbv, current_view)
        if summary:
            return summary
    summary = None
    if render_child_summary is None:
        summary = sbv.GetSummary()
    if summary:
        return summary
    value = sbv.GetValue()
    if value is not None:
        return value
    return "{...}"
