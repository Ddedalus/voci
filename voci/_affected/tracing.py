"""A `Tracer`'s lifetime as a context manager, for the parent's own run -- the same start/stop
shape `_isolated_worker.py` already uses per subprocess, factored out so `cli.py`'s own driver can
share it without duplicating the `try`/`finally`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from voci._affected import collector as _collector
from voci._affected.tracer import Tracer

__all__ = ["traced"]


@contextlib.contextmanager
def traced(rootdir: Path) -> Iterator[str | None]:
    """Start a `Tracer` over `rootdir` for the duration of the `with` block, yielding
    `Tracer.start`'s own result unchanged: `None` on success, or the reason a caller should fall
    back to a full run (Selection design section: "no tool id is free") if every candidate id was
    already taken. `tracer.stop()` always runs on the way out, success or not -- the one thing a
    bare `Tracer()`/`.start()` call leaves to the caller to get right on every exit path.
    """
    tracer = Tracer(rootdir, _collector.record_first_party)
    reason = tracer.start()
    try:
        yield reason
    finally:
        tracer.stop()
