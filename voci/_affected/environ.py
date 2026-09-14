"""M4's `os.environ` recorder (`plans/affected-tests-plan.md`, Non-code dependencies design
section): each environment variable a test or fixture reads becomes an `EnvKey` (`resolve.py`),
checksummed by `store.env_checksum` as a hash of its current value, or "absent".

`os.environ` is swapped to a recording subclass for the run" is the design section's own wording,
but the probe behind it found something narrower and cheaper to reach for: `os.getenv`, `.get()`,
`in`, `.copy()` and iteration all end up calling `os.environ.__class__.__getitem__` under the hood
(`MutableMapping`'s own mixins implement each of those in terms of `__getitem__`), so patching that
one method on `type(os.environ)` -- not swapping `os.environ` itself for a different object --
already observes every one of them. Patching the *class*, not the instance, is also what keeps
`os.environ is os.environ` true throughout a run for any code that already cached the reference,
which a full swap would break.

Same idempotence shape as `threads.py`: `install()`/`uninstall()` are a matched pair a caller
always runs in a `try`/`finally`, `install()` reporting whether *this* call is the one that
patched anything so a nested caller's `uninstall()` doesn't tear down an outer one's patch early.
Unlike `threads.py`'s opt-in patches, this one carries no observable behavior change -- every read
still returns exactly what it always would -- so it isn't gated behind a `[tool.voci]` key; see the
Non-code dependencies design section's own framing of the audit hook next to it as "installed...
it's a no-op without a collector", the same unconditional posture this module takes.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from voci._affected.collector import current_collector

__all__ = ["install", "uninstall"]

#: `None` when not installed; the original unbound method to restore otherwise -- same
#: module-global shape as `threads.py`'s own `_original_thread_start`. `Any` rather than
#: `_Environ`, which isn't part of `os`'s public type surface: `type(os.environ).__getitem__` is
#: generic over `str`/`bytes` (`os.environ` vs. `os.environb`), a distinction this module doesn't
#: need to spell out in its own type just to store the one bound-off method it patches.
_original_getitem: Callable[[Any, str], str] | None = None


def install() -> bool:
    """Patch `type(os.environ).__getitem__` for the duration of a run. Returns whether *this*
    call installed it, `False` if an enclosing call already had."""
    global _original_getitem
    if _original_getitem is not None:
        return False
    cls = type(os.environ)
    _original_getitem = cls.__getitem__
    cls.__getitem__ = _recording_getitem  # type: ignore[method-assign]
    return True


def uninstall() -> None:
    """Undo `install()`. A no-op if it was never called, so a caller can always run this in a
    `finally` without checking whether `install()` succeeded, or ran at all, first."""
    global _original_getitem
    if _original_getitem is not None:
        type(os.environ).__getitem__ = _original_getitem  # type: ignore[method-assign]
        _original_getitem = None


def _recording_getitem(self: Any, key: str) -> str:
    """Record `key` against whichever collector is current -- before delegating, not after, so a
    lookup that raises `KeyError` (the variable is unset) still counts as a read of it; `store.
    env_checksum` treats that the same as any other value, hashing "absent" instead. `os.environb`
    shares this same patched class on POSIX but is keyed by `bytes`, not `str`; a non-`str` key
    is passed straight through unrecorded rather than added to a `frozenset[str]` it doesn't
    belong in."""
    assert _original_getitem is not None  # install() always sets this before patching
    if isinstance(key, str):
        collector = current_collector.get()
        if collector is not None:
            collector.record_env(key)
    return _original_getitem(self, key)
