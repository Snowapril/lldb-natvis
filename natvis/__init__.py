"""natvis - Visual Studio .natvis driven formatters for LLDB."""

import logging

__version__ = "0.1.0"

log = logging.getLogger("natvis")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("natvis: %(levelname)s: %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.WARNING)


def set_verbose(enabled):
    log.setLevel(logging.DEBUG if enabled else logging.WARNING)
