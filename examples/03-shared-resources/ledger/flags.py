"""A process-global feature flag registry.

Module-level mutable state read at call time. There is no per-task view of it, no ContextVar
underneath, and no way to give two concurrent tests different answers. A test that flips a flag
must therefore run alone — this is the canonical `@velox.solo` case, and it has nothing to do with
mocking.
"""

from __future__ import annotations

_FLAGS: dict[str, bool] = {
    "strict_transfers": False,
    "audit_every_write": False,
}


def is_enabled(name: str) -> bool:
    return _FLAGS.get(name, False)


def set_enabled(name: str, value: bool) -> None:
    _FLAGS[name] = value


def snapshot() -> dict[str, bool]:
    return dict(_FLAGS)


def restore(state: dict[str, bool]) -> None:
    _FLAGS.clear()
    _FLAGS.update(state)
