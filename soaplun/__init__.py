"""Soap à l'un - Zimbra provisioning and mailbox migration tool."""

from pathlib import Path as _Path
import sys as _sys

__version__ = "0.1.0"

# The vendored Zimbra libraries live outside the package so they stay easy to
# replace.  Add them to sys.path once, at import time.
_VENDOR = _Path(__file__).resolve().parent.parent / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in _sys.path:
    _sys.path.insert(0, str(_VENDOR))
