"""Central registry: loaded natvis files, type matching/dispatch, and LLDB
category registration.  All registered specifiers route to the single summary
function / synthetic class; the exact natvis Type (and $T bindings) is picked
here in Python."""

import os
from collections import OrderedDict

import lldb

from . import log
from .expr import preprocess
from .parser import parse_file
from .typename import (
    has_wildcard, literal_len, match_type, normalize, pattern_to_regex,
    wildcard_count,
)

CATEGORY = "natvis"
_SUMMARY_FUNC = "lldb_natvis.natvis_summary"
_SYNTH_CLASS = "lldb_natvis.NatvisSyntheticProvider"
_OVERRIDE_CAP = 4096


class Resolved:
    """A dispatch result: the matched natvis Type, bound $T args, and the value
    (possibly cast to a base class).  When `synthetic` is set, this describes a
    <Synthetic> pseudo-member and its nested DisplayString/Expand apply."""

    __slots__ = ("ntype", "bindings", "val", "synthetic")

    def __init__(self, ntype, bindings, val, synthetic=None):
        self.ntype = ntype
        self.bindings = tuple(bindings)
        self.val = val
        self.synthetic = synthetic

    def display_strings(self):
        if self.synthetic is not None:
            return self.synthetic.display_strings
        return self.ntype.display_strings

    def string_views(self):
        if self.synthetic is not None:
            return self.synthetic.string_views
        return self.ntype.string_views

    def expand(self):
        if self.synthetic is not None:
            return self.synthetic.expand
        return self.ntype.expand

    def intrinsics(self):
        return self.ntype.intrinsics

    def prep(self, expr):
        return preprocess(expr, self.bindings, self.ntype.intrinsics)


class NatvisRegistry:
    def __init__(self):
        self.debugger = None
        self.files = OrderedDict()          # path -> NatvisFile
        self.exact = {}                     # normalized name -> [(NatvisType, pattern)]
        self.wildcards = []                 # (compiled_regex, pattern, NatvisType)
        self.resolve_cache = {}
        self._match_cache = {}
        self.synthetic_overrides = OrderedDict()
        self.max_children = 1000

    # ------------------------------------------------------------- loading

    def attach(self, debugger):
        self.debugger = debugger

    def load_path(self, path):
        """Load a .natvis file or every *.natvis in a directory.
        Returns list of NatvisFile."""
        loaded = []
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):
            for entry in sorted(os.listdir(path)):
                if entry.lower().endswith(".natvis"):
                    loaded.append(self._load_file(os.path.join(path, entry)))
        elif os.path.isfile(path):
            loaded.append(self._load_file(path))
        else:
            raise IOError("no such file or directory: %s" % path)
        if loaded:
            self.refresh()
        return loaded

    def _load_file(self, path):
        nf = parse_file(path)
        self.files[path] = nf
        for err in nf.errors:
            log.warning("%s", err)
        log.debug("loaded %s: %d types", path, len(nf.types))
        return nf

    def unload(self, path=None):
        if path is None:
            self.files.clear()
        else:
            path = os.path.abspath(os.path.expanduser(path))
            self.files.pop(path, None)
        self.refresh()

    def reload(self):
        paths = list(self.files.keys())
        self.files.clear()
        for path in paths:
            if os.path.exists(path):
                self._load_file(path)
        self.refresh()

    def refresh(self):
        self._rebuild_index()
        self._register_with_lldb()

    # ------------------------------------------------------------ indexing

    def _rebuild_index(self):
        self.exact = {}
        self.wildcards = []
        self.resolve_cache = {}
        self._match_cache = {}
        self.synthetic_overrides.clear()
        match_type.cache_clear()
        for load_idx, nf in enumerate(self.files.values()):
            for ntype in nf.types:
                ntype._load_idx = load_idx
                for pattern in ntype.names:
                    if has_wildcard(pattern):
                        try:
                            import re as _re
                            regex = _re.compile(pattern_to_regex(pattern))
                        except Exception as exc:
                            log.warning("bad pattern %r: %s", pattern, exc)
                            continue
                        self.wildcards.append((regex, pattern, ntype))
                    else:
                        key = normalize(pattern)
                        self.exact.setdefault(key, []).append((ntype, pattern))

    def _register_with_lldb(self):
        """One callback-matched summary + synthetic pair routes everything
        through the Python-side resolver (handles wildcards, typedefs, AND
        base-class inheritance, which lldb name/regex specifiers cannot)."""
        dbg = self.debugger
        if dbg is None:
            return
        dbg.DeleteCategory(CATEGORY)
        if not self.files:
            return
        cat = dbg.CreateCategory(CATEGORY)
        summ = lldb.SBTypeSummary.CreateWithFunctionName(
            _SUMMARY_FUNC, lldb.eTypeOptionCascade)
        synth = lldb.SBTypeSynthetic.CreateWithClassName(
            _SYNTH_CLASS, lldb.eTypeOptionCascade)
        cat.AddTypeSummary(
            lldb.SBTypeNameSpecifier("lldb_natvis.natvis_match_summary",
                                     lldb.eFormatterMatchCallback), summ)
        cat.AddTypeSynthetic(
            lldb.SBTypeNameSpecifier("lldb_natvis.natvis_match_synthetic",
                                     lldb.eFormatterMatchCallback), synth)
        cat.SetEnabled(True)

    # ---------------------------------------------- formatter match callbacks

    def match_sbtype(self, sbtype, want_expand):
        """Formatter-callback matching: does any natvis Type apply to sbtype
        (directly or via an Inheritable base class)?"""
        if sbtype is None or not sbtype.IsValid():
            return False
        if sbtype.IsPointerType() or sbtype.IsReferenceType():
            return False
        key = (sbtype.GetName() or "", want_expand)
        cached = self._match_cache.get(key)
        if cached is not None:
            return cached
        result = self._match_sbtype_uncached(sbtype, want_expand)
        self._match_cache[key] = result
        return result

    def _match_sbtype_uncached(self, sbtype, want_expand, depth=0):
        names = []
        for candidate in (sbtype.GetUnqualifiedType().GetName(),
                          sbtype.GetName(),
                          sbtype.GetCanonicalType().GetName()):
            if candidate and candidate not in names:
                names.append(candidate)
        for name in names:
            r = self.resolve_name(name)
            if r is not None:
                if depth > 0 and not r[0].inheritable:
                    continue
                ntype = r[0]
                if want_expand:
                    return ntype.has_expand
                # a type with only <Expand> still hides nothing summary-wise
                return ntype.has_summary
        if depth >= 8:
            return False
        canonical = sbtype.GetCanonicalType()
        for i in range(canonical.GetNumberOfDirectBaseClasses()):
            base = canonical.GetDirectBaseClassAtIndex(i).GetType()
            if self._match_sbtype_uncached(base, want_expand, depth + 1):
                return True
        return False

    # ------------------------------------------------------------ dispatch

    def resolve_name(self, type_name):
        """Best (NatvisType, bindings) for a concrete type name, or None."""
        if not type_name:
            return None
        norm = normalize(type_name)
        if norm in self.resolve_cache:
            return self.resolve_cache[norm]
        candidates = []
        for ntype, pattern in self.exact.get(norm, []):
            candidates.append((ntype, (), pattern))
        for regex, pattern, ntype in self.wildcards:
            if not regex.match(norm) and not regex.match(type_name):
                continue
            bindings = match_type(pattern, type_name)
            if bindings is not None:
                candidates.append((ntype, bindings, pattern))
        result = None
        if candidates:
            candidates.sort(key=lambda c: (
                -c[0].priority_rank,
                wildcard_count(c[2]),
                -literal_len(c[2]),
                c[0]._load_idx,
                c[0].source_order,
            ))
            best = candidates[0]
            result = (best[0], best[1])
        self.resolve_cache[norm] = result
        return result

    def resolve_for_value(self, val):
        """Resolve an SBValue to a Resolved dispatch record, or None."""
        if val is None or not val.IsValid():
            return None
        t = val.GetType()
        if not t.IsValid():
            return None
        if t.IsReferenceType():
            deref = val.Dereference()
            if deref.IsValid():
                val = deref
                t = val.GetType()
        if t.IsPointerType():
            return None
        # <Synthetic> pseudo-member override?  Only values we materialized
        # ourselves (CreateValueFromAddress -> eValueTypeConstResult) may hit
        # this map; a real variable/member that happens to share the synthetic
        # member's name, address, and type must keep its normal formatting.
        if val.GetValueType() == lldb.eValueTypeConstResult:
            okey = (val.GetLoadAddress(), val.GetName() or "",
                    t.GetName() or "")
            override = self.synthetic_overrides.get(okey)
            if override is not None:
                syn, ntype, bindings = override
                return Resolved(ntype, bindings, val, synthetic=syn)
        names = []
        for candidate in (t.GetUnqualifiedType().GetName(), t.GetName(),
                          t.GetCanonicalType().GetName()):
            if candidate and candidate not in names:
                names.append(candidate)
        for name in names:
            r = self.resolve_name(name)
            if r is not None:
                return Resolved(r[0], r[1], val)
        return self._resolve_via_bases(val, t)

    def _resolve_via_bases(self, val, t, depth=0):
        if depth >= 8:
            return None
        canonical = t.GetCanonicalType()
        for i in range(canonical.GetNumberOfDirectBaseClasses()):
            base = canonical.GetDirectBaseClassAtIndex(i)
            base_type = base.GetType()
            name = base_type.GetName()
            r = self.resolve_name(name) if name else None
            if r is not None and r[0].inheritable:
                cast = val.Cast(base_type)
                if cast.IsValid():
                    return Resolved(r[0], r[1], cast)
            deeper = self._resolve_via_bases(val, base_type, depth + 1)
            if deeper is not None:
                return deeper
        return None

    # --------------------------------------------- <Synthetic> override map

    def register_synthetic_override(self, addr, name, type_name, syn, ntype,
                                    bindings):
        key = (addr, name, type_name)
        self.synthetic_overrides[key] = (syn, ntype, tuple(bindings))
        while len(self.synthetic_overrides) > _OVERRIDE_CAP:
            self.synthetic_overrides.popitem(last=False)


REGISTRY = NatvisRegistry()
