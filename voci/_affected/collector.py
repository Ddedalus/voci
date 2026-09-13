"""What a `Tracer`'s `on_first_party` callback attributes running code to (see
`plans/affected-tests-plan.md`, Tracer's "Recording" and "Which collector" bullets).

Three collectors exist at once over the life of a run, nested by `ContextVar` the way
`_capture.current_test_context` is (`plans/rationale/global.md`, "Everything per-test lives in a
ContextVar"): a test's own, spanning its whole setup/call/teardown envelope
(`_run.run._Session.run_envelope`); a `module`/`session`-scope fixture's own, spanning its
construction *and* its later teardown, which can run on an unrelated task, or with no test's
envelope active at all (`_di.runtime.ScopeStore`); and a collection collector, spanning one
file's import (`_collection.collect._import_module`). Whichever was `active()`d most recently
for the running task is "current" -- a module fixture built during a test's setup shadows that
test's own collector for the span of its construction, then hands control back on `reset`.

Nothing calls `Tracer` yet (see `_affected/__init__.py`), so `record_first_party` below is not
wired to it either; only the three call sites' own open/close bookkeeping runs so far.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from types import CodeType

__all__ = ["Collector", "active", "current_collector", "record_first_party"]


class Collector:
    """Every first-party code object that ran while this collector was current.

    Keyed by `id(code)` -- a keep-alive dict, not a bare set of ids, because nothing else is
    guaranteed to hold a reference to a code object for as long as this collector needs one: a
    `<lambda>` or comprehension body has no other owner once the statement that made it returns.
    """

    __slots__ = ("codes",)

    def __init__(self) -> None:
        self.codes: dict[int, CodeType] = {}

    def record(self, code: CodeType) -> None:
        self.codes[id(code)] = code


current_collector: ContextVar[Collector | None] = ContextVar("voci_current_collector", default=None)


@contextlib.contextmanager
def active(collector: Collector) -> Iterator[None]:
    """Make `collector` the current one for the duration of the `with` block, restoring
    whatever was current before -- a test's own collector, if this is a fixture's or a file's
    span nested inside one, or nothing at all (session-scope teardown runs with no ambient
    collector at all; see `_di.runtime.ScopeStore.aclose`)."""
    token = current_collector.set(collector)
    try:
        yield
    finally:
        current_collector.reset(token)


def record_first_party(code: CodeType) -> None:
    """`Tracer`'s `on_first_party` callback, once something starts calling it: attribute `code`
    to whichever collector is current, or drop it if none is -- temporary, until the next M1
    bullet turns "first-party code ran with nothing to attribute it to" into marking the test
    untrusted instead of silently losing the record.
    """
    collector = current_collector.get()
    if collector is not None:
        collector.record(code)
