"""A `Tracer`'s lifetime as a context manager, for the parent's own run -- the same start/stop
shape `_isolated_worker.py` already uses per subprocess, factored out so `cli.py`'s own driver can
share it without duplicating the `try`/`finally` (`plans/affected-tests-plan.md`, M3's still-
missing "a real `Tracer` around the parent's own run" bullet).

M4's audit hook and `os.environ` recorder share this same lifetime: both are as inert as a `Tracer`
with no free tool id would leave code recording (Non-code dependencies design section: the audit
hook is "a no-op without a collector"), so there's nothing to gain from a separate context manager
-- one `with traced(rootdir):` around collection and the run, in both the parent (`cli.py`) and
`@voci.isolated`'s own subprocess (`_isolated_worker.py`), turns on every M1-M4 recording mechanism
together and always tears all three down on the way out, exception or not.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from pathlib import Path

from voci._affected import audit as _audit
from voci._affected import collector as _collector
from voci._affected import environ as _environ
from voci._affected.tracer import Tracer

__all__ = ["traced"]


@contextlib.contextmanager
def traced(rootdir: Path) -> Iterator[str | None]:
    """Start a `Tracer` over `rootdir`, plus the audit hook and `os.environ` recorder, for the
    duration of the `with` block, yielding `Tracer.start`'s own result unchanged: `None` on
    success, or the reason a caller should fall back to a full run (Selection design section: "no
    tool id is free") if every candidate id was already taken. All three are torn down on the way
    out regardless -- the one thing a bare `Tracer()`/`.start()` call, or `audit.install`/`environ.
    install` on their own, leave to the caller to get right on every exit path. The audit hook's
    own `session_start` is "now", at the moment this call is entered -- before collection ever
    imports a test file, so a file written by a fixture partway through this run is never mistaken
    for a dependency this run should have recorded.
    """
    tracer = Tracer(rootdir, _collector.record_first_party)
    reason = tracer.start()
    audit_token = _audit.install(rootdir, session_start=time.time())
    environ_installed = _environ.install()
    try:
        yield reason
    finally:
        if environ_installed:
            _environ.uninstall()
        _audit.uninstall(audit_token)
        tracer.stop()
