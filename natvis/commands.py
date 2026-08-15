"""The `natvis` LLDB command: load/reload/list/unload/verbose/stringview/status."""

import os
import shlex

from . import __version__, log, set_verbose
from . import expr as expr_mod
from .registry import REGISTRY

_HELP = """natvis %s - Visual Studio .natvis formatters for LLDB
Subcommands:
  natvis load <file-or-dir>   load a .natvis file (or all *.natvis in a dir)
  natvis reload               re-parse and re-register all loaded files
  natvis list [-v]            show loaded files and their types
  natvis unload [<file>|--all]
  natvis verbose on|off       toggle debug logging
  natvis stringview <expr>    print the StringView content of a value
  natvis status               cache / eval-path statistics
""" % __version__


class NatvisCommand:
    def __init__(self, debugger, internal_dict):
        pass

    def get_short_help(self):
        return "Visual Studio .natvis formatters for LLDB"

    def get_long_help(self):
        return _HELP

    def __call__(self, debugger, command, exe_ctx, result):
        try:
            args = shlex.split(command)
        except ValueError as exc:
            result.SetError(str(exc))
            return
        if not args:
            result.AppendMessage(_HELP)
            return
        sub = args.pop(0)
        handler = getattr(self, "_cmd_" + sub.replace("-", "_"), None)
        if handler is None:
            result.SetError("unknown subcommand %r\n%s" % (sub, _HELP))
            return
        try:
            handler(debugger, args, exe_ctx, result)
        except Exception as exc:
            result.SetError("natvis %s failed: %s" % (sub, exc))

    # ------------------------------------------------------------------

    def _cmd_load(self, debugger, args, exe_ctx, result):
        if not args:
            result.SetError("usage: natvis load <file-or-dir>")
            return
        REGISTRY.attach(debugger)
        total = 0
        for path in args:
            for nf in REGISTRY.load_path(path):
                total += len(nf.types)
                result.AppendMessage("loaded %s (%d types%s)" % (
                    nf.path, len(nf.types),
                    ", %d warnings" % len(nf.errors) if nf.errors else ""))
        result.AppendMessage("%d natvis type(s) active" % total)

    def _cmd_reload(self, debugger, args, exe_ctx, result):
        REGISTRY.attach(debugger)
        REGISTRY.reload()
        result.AppendMessage("reloaded %d file(s)" % len(REGISTRY.files))

    def _cmd_list(self, debugger, args, exe_ctx, result):
        verbose = "-v" in args
        if not REGISTRY.files:
            result.AppendMessage("no natvis files loaded")
            return
        for path, nf in REGISTRY.files.items():
            result.AppendMessage("%s: %d types, %d warnings"
                                 % (path, len(nf.types), len(nf.errors)))
            if verbose:
                for err in nf.errors:
                    result.AppendMessage("  warning: %s" % err)
                for ntype in nf.types:
                    result.AppendMessage("  %s (priority=%s%s%s)" % (
                        ", ".join(ntype.names), ntype.priority,
                        ", summary" if ntype.has_summary else "",
                        ", expand" if ntype.has_expand else ""))

    def _cmd_unload(self, debugger, args, exe_ctx, result):
        REGISTRY.attach(debugger)
        if not args or args[0] == "--all":
            REGISTRY.unload()
            result.AppendMessage("unloaded all natvis files")
        else:
            for path in args:
                REGISTRY.unload(path)
            result.AppendMessage("unloaded %s" % ", ".join(args))

    def _cmd_verbose(self, debugger, args, exe_ctx, result):
        enable = not args or args[0] != "off"
        set_verbose(enable)
        result.AppendMessage("natvis verbose logging %s"
                             % ("on" if enable else "off"))

    def _cmd_stringview(self, debugger, args, exe_ctx, result):
        if not args:
            result.SetError("usage: natvis stringview <expr>")
            return
        frame = exe_ctx.GetFrame()
        if not frame.IsValid():
            result.SetError("no frame; run to a breakpoint first")
            return
        val = frame.GetValueForVariablePath(args[0])
        if not val.IsValid() or val.GetError().Fail():
            val = frame.EvaluateExpression(" ".join(args))
        if not val.IsValid() or val.GetError().Fail():
            result.SetError("cannot evaluate %r" % " ".join(args))
            return
        from .display import stringview_text
        text = stringview_text(val)
        if text is None:
            result.SetError("no StringView for type %s" % val.GetTypeName())
        else:
            result.AppendMessage(text)

    def _cmd_status(self, debugger, args, exe_ctx, result):
        counters = expr_mod.counters()
        result.AppendMessage("files loaded : %d" % len(REGISTRY.files))
        types = sum(len(nf.types) for nf in REGISTRY.files.values())
        result.AppendMessage("types        : %d" % types)
        result.AppendMessage("resolve cache: %d entries"
                             % len(REGISTRY.resolve_cache))
        result.AppendMessage(
            "eval paths   : fast=%d int=%d slow=%d slow_fail=%d" % (
                counters["fast"], counters["int"], counters["slow"],
                counters["slow_fail"]))
        result.AppendMessage("max children : %d" % REGISTRY.max_children)


def autoscan(debugger):
    """Load *.natvis from cwd, NATVIS_PATH entries, and target exe dirs."""
    REGISTRY.attach(debugger)
    candidates = [os.getcwd()]
    env_path = os.environ.get("NATVIS_PATH")
    if env_path:
        candidates.extend(p for p in env_path.split(":") if p)
    for i in range(debugger.GetNumTargets()):
        target = debugger.GetTargetAtIndex(i)
        exe = target.GetExecutable()
        if exe and exe.GetDirectory():
            candidates.append(exe.GetDirectory())
    seen = set()
    found = 0
    for cand in candidates:
        cand = os.path.abspath(cand)
        if cand in seen or not os.path.isdir(cand):
            continue
        seen.add(cand)
        try:
            entries = sorted(os.listdir(cand))
        except OSError:
            continue
        if any(e.lower().endswith(".natvis") for e in entries):
            for nf in REGISTRY.load_path(cand):
                found += len(nf.types)
    return found
