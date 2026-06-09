"""DEPRECATED: skcomm is now skcomms.

This package is a thin re-export shim. All transport modules now live in
``skcomms``. This shim will be removed in a future release.

Migrate your imports::

    # OLD (deprecated)
    from skcomm.core import SKComm
    from skcomm.models import MessageEnvelope

    # NEW (canonical)
    from skcomms.core import SKComm
    from skcomms.models import MessageEnvelope
"""
import importlib
import sys
import warnings

warnings.warn(
    "skcomm is deprecated — import from skcomms instead",
    DeprecationWarning,
    stacklevel=2,
)
_pkg = importlib.import_module("skcomms")
# Alias the parent package → submodule imports use skcomms.__path__
sys.modules[__name__] = _pkg
