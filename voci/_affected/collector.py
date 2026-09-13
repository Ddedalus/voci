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

`record_first_party` is `Tracer.on_first_party`'s eventual callback (nothing calls `Tracer` yet,
see `_affected/__init__.py`): first-party code with a current collector is attributed to it, same
as always. First-party code with *no* current collector -- an unattributed thread or executor
worker running while tests are still in flight -- can't say which of them it belongs to, so it
marks *every* collector currently `active()` anywhere untrusted instead: conservative, but sound,
since an untrusted test always runs (see the plan's "Self-declared untrusted" and Selection).
`_affected.threads`' opt-in patches are what keep a collector current across a thread or executor
hop in the first place, so this path is only hit without them, or for a case they don't cover
(a bare `os.fork`-based worker, a C-started thread, ...).
"""

from __future__ import annotations

import contextlib
import threading
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

    __slots__ = ("codes", "untrusted")

    def __init__(self) -> None:
        self.codes: dict[int, CodeType] = {}
        self.untrusted: str | None = None
        """`None` while this collector is still trustworthy; otherwise the reason the *first*
        thing to distrust it gave. Later marks are recorded nowhere else -- a stored record has
        one untrusted flag, not a list of reasons, and the first cause is the one worth keeping:
        it's what happened earliest, so it's what a later mark (a second unattributed thread, a
        `@voci.untrusted` on top of an automatic mark) can't explain any better than it already
        does."""

    def record(self, code: CodeType) -> None:
        self.codes[id(code)] = code

    def mark_untrusted(self, reason: str) -> None:
        """Distrust everything this collector recorded, with `reason` for whoever reads it next.
        Idempotent past the first call -- see `untrusted`'s own docstring for why the first
        reason wins rather than the latest. Locked on the module's shared `_in_flight_lock`,
        not a lock of its own, because the only concurrent caller today is
        `record_first_party`'s in-flight loop -- two threads racing to distrust the same
        collector at once -- and a check-then-set with no lock at all would let the later of the
        two overwrite the earlier one's reason instead of losing to it."""
        with _in_flight_lock:
            if self.untrusted is None:
                self.untrusted = reason


current_collector: ContextVar[Collector | None] = ContextVar("voci_current_collector", default=None)

#: Every collector currently inside its own `active()` block, anywhere -- across every task and
#: thread, not just the one `current_collector` reads for the caller's own context. Refcounted by
#: `id(collector)` rather than a bare set: a `module`/`session`-scope entry's collector can be
#: `active()` for its construction and, later, its teardown, and either span could -- in
#: principle -- nest inside itself if a fixture's own teardown recursed into another `active()`
#: call on the same collector. A `threading.Lock`, not just relying on the GIL, because
#: `dict.__setitem__` on an existing key is a read-modify-write from Python's point of view, not
#: the single bytecode a bare insert would be.
_in_flight_lock = threading.Lock()
_in_flight_refcounts: dict[int, int] = {}
_in_flight_collectors: dict[int, Collector] = {}


@contextlib.contextmanager
def active(collector: Collector) -> Iterator[None]:
    """Make `collector` the current one for the duration of the `with` block, restoring
    whatever was current before -- a test's own collector, if this is a fixture's or a file's
    span nested inside one, or nothing at all (session-scope teardown runs with no ambient
    collector at all; see `_di.runtime.ScopeStore.aclose`). Also registers `collector` as
    in-flight for the same span, for `record_first_party` to find from a context that has no
    current collector of its own."""
    token = current_collector.set(collector)
    try:
        key = id(collector)
        with _in_flight_lock:
            _in_flight_refcounts[key] = _in_flight_refcounts.get(key, 0) + 1
            _in_flight_collectors[key] = collector
        try:
            yield
        finally:
            with _in_flight_lock:
                _in_flight_refcounts[key] -= 1
                if _in_flight_refcounts[key] <= 0:
                    del _in_flight_refcounts[key]
                    del _in_flight_collectors[key]
    finally:
        # Its own finally, outermost: current_collector must be reset even if registering or
        # unregistering the in-flight entry above raised, so a mid-`active()` exception can never
        # leave current_collector permanently pointing at a collector no one is using anymore.
        current_collector.reset(token)


def record_first_party(code: CodeType) -> None:
    """`Tracer`'s `on_first_party` callback, once something starts calling it: attribute `code`
    to whichever collector is current, or -- if none is -- distrust every collector that's
    in-flight anywhere, since `code` ran on behalf of one of them but which one can't be told
    from here. With nothing in flight at all (code running before any test starts, or the
    ordinary third-party/stdlib case `Tracer` never even calls this for), there's nothing to
    distrust and this is a no-op.
    """
    collector = current_collector.get()
    if collector is not None:
        collector.record(code)
        return
    reason = (
        f"first-party code ran on an unattributed thread: {code.co_qualname} "
        f"({code.co_filename}:{code.co_firstlineno})"
    )
    with _in_flight_lock:
        in_flight = list(_in_flight_collectors.values())
    for c in in_flight:
        c.mark_untrusted(reason)
