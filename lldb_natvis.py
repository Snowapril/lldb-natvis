"""LLDB entry point for natvis-driven formatters.

Usage (in ~/.lldbinit, works for both CLI lldb and Xcode):
    command script import /path/to/lldb-natvis/lldb_natvis.py

On import this auto-loads every *.natvis found in the current working
directory, in $NATVIS_PATH entries (colon-separated), and next to any existing
target executables.  Use `natvis load <file-or-dir>` for everything else.
"""

import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from natvis import commands as _commands          # noqa: E402
from natvis import display as _display            # noqa: E402
from natvis import providers as _providers        # noqa: E402
from natvis.registry import REGISTRY              # noqa: E402


def natvis_summary(valobj, internal_dict):
    """Summary entry point resolved by LLDB as 'lldb_natvis.natvis_summary'."""
    return _display.natvis_summary(valobj, internal_dict)


def natvis_match_summary(sbtype, internal_dict):
    """Formatter match callback: does a natvis DisplayString apply to sbtype?"""
    try:
        return REGISTRY.match_sbtype(sbtype, want_expand=False)
    except Exception:
        return False


def natvis_match_synthetic(sbtype, internal_dict):
    """Formatter match callback: does a natvis <Expand> apply to sbtype?"""
    try:
        return REGISTRY.match_sbtype(sbtype, want_expand=True)
    except Exception:
        return False


class NatvisSyntheticProvider(_providers.NatvisSyntheticProvider):
    """Synthetic provider resolved as 'lldb_natvis.NatvisSyntheticProvider'."""


NatvisCommand = _commands.NatvisCommand


def __lldb_init_module(debugger, internal_dict):
    REGISTRY.attach(debugger)
    debugger.HandleCommand(
        "command script add -c lldb_natvis.NatvisCommand natvis")
    found = _commands.autoscan(debugger)
    if found:
        print("natvis: %d type visualizer(s) loaded (see `natvis list`)"
              % found)
    else:
        print("natvis: ready; load files with `natvis load <file-or-dir>`")
