"""if TYPE_CHECKING import, try/except conditional definition, __all__,
del."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import User as _UserForTyping

try:
    import json as _fast_json
except ImportError:
    _fast_json = None

__all__ = ["public_fn"]

_secret = 1
del _secret


def public_fn():
    return _fast_json
