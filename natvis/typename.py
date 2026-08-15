"""Template type-name handling: natvis wildcard patterns -> loose regexes for
LLDB registration, plus an exact structural matcher that binds $T1..$Tn."""

import re
from functools import lru_cache


def normalize(name):
    """Canonical whitespace form used for all comparisons."""
    if name is None:
        return ""
    s = name.strip()
    # drop cv-qualifiers at top level only (cheap heuristic: leading/trailing)
    s = re.sub(r"^(const|volatile)\s+", "", s)
    s = re.sub(r"\s+(const|volatile)$", "", s)
    s = re.sub(r"\s+", " ", s)
    # no spaces around punctuation
    s = re.sub(r"\s*([<>,*&()\[\]])\s*", r"\1", s)
    s = s.replace(">>", "> >")  # canonical: single space between closers
    s = re.sub(r"\s*(::)\s*", r"\1", s)
    s = s.rstrip("&")   # reference-ness never participates in matching
    return s


def has_wildcard(pattern):
    return "*" in pattern


def pattern_to_regex(pattern):
    """Loose superset regex for LLDB registration.  Exactness is enforced later
    by match_type() in Python, so this only needs to never miss a real match."""
    chunks = pattern.split("*")
    parts = []
    for chunk in chunks:
        esc = re.escape(chunk)
        # tolerate arbitrary whitespace around template punctuation
        esc = esc.replace(re.escape("<"), r"<\s*")
        esc = esc.replace(re.escape(">"), r"\s*>")
        esc = esc.replace(re.escape(","), r"\s*,\s*")
        parts.append(esc)
    return "^" + r".*".join(parts) + "$"


def split_template(name):
    """'Foo<A, Bar<B>>' -> ('Foo', ['A', 'Bar<B>']).  Non-template -> (name, None).
    Returns None if brackets are unbalanced."""
    lt = name.find("<")
    if lt < 0:
        return (name, None)
    if not name.endswith(">"):
        return (name, None)
    base = name[:lt]
    inner = name[lt + 1:-1]
    args = []
    depth = 0
    start = 0
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            if ch == ">" and i > 0 and inner[i - 1] == "-":
                pass  # '->' operator, not a bracket
            else:
                depth -= 1
                if depth < 0:
                    return None
        elif ch == "," and depth == 0:
            args.append(inner[start:i].strip())
            start = i + 1
        i += 1
    args.append(inner[start:].strip())
    return (base, args)


def _match_segments(pattern, concrete, bindings):
    """Recursive structural match. Appends wildcard bindings in document order.
    Returns True on match."""
    pattern = pattern.strip()
    concrete = concrete.strip()
    if pattern == "*":
        bindings.append(concrete)
        return True
    ps = split_template(pattern)
    cs = split_template(concrete)
    if ps is None or cs is None:
        return False
    pbase, pargs = ps
    cbase, cargs = cs
    if pbase != cbase:
        return False
    if pargs is None:
        return cargs is None
    if cargs is None:
        return False
    pi = 0
    for pi, parg in enumerate(pargs):
        last = pi == len(pargs) - 1
        if last and parg == "*":
            # trailing * absorbs one-or-more remaining args, binding each
            rest = cargs[pi:]
            if not rest:
                return False
            bindings.extend(rest)
            return True
        if pi >= len(cargs):
            return False
        if not _match_segments(parg, cargs[pi], bindings):
            return False
    return len(pargs) == len(cargs)


@lru_cache(maxsize=8192)
def match_type(pattern, concrete):
    """Match a natvis Name pattern against a concrete type name.

    Returns a tuple of bound template-arg strings ($T1..$Tn, in wildcard
    document order) on success, or None on mismatch."""
    bindings = []
    if _match_segments(normalize(pattern), normalize(concrete), bindings):
        return tuple(bindings)
    return None


def wildcard_count(pattern):
    return pattern.count("*")


def literal_len(pattern):
    return len(pattern.replace("*", ""))
