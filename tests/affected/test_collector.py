"""Tests for voci._affected.collector: what a Tracer's first-party callback attributes running
code to."""

from __future__ import annotations

import threading

from voci._affected import collector


def _sample() -> None:
    pass


def test_collector_records_a_code_object_keyed_by_its_id() -> None:
    c = collector.Collector()
    code = _sample.__code__
    c.record(code)
    assert c.codes == {id(code): code}


def test_recording_the_same_code_object_twice_is_idempotent() -> None:
    c = collector.Collector()
    code = _sample.__code__
    c.record(code)
    c.record(code)
    assert c.codes == {id(code): code}


def test_current_collector_defaults_to_none() -> None:
    assert collector.current_collector.get() is None


def test_active_makes_a_collector_current_and_restores_none_after() -> None:
    c = collector.Collector()
    with collector.active(c):
        assert collector.current_collector.get() is c
    assert collector.current_collector.get() is None


def test_active_nests_and_restores_the_outer_collector_afterward() -> None:
    outer = collector.Collector()
    inner = collector.Collector()
    with collector.active(outer):
        with collector.active(inner):
            assert collector.current_collector.get() is inner
        assert collector.current_collector.get() is outer
    assert collector.current_collector.get() is None


def test_record_first_party_records_into_whichever_collector_is_current() -> None:
    c = collector.Collector()
    code = _sample.__code__
    with collector.active(c):
        collector.record_first_party(code)
    assert c.codes == {id(code): code}


def test_record_first_party_is_a_no_op_with_nothing_in_flight() -> None:
    # Must not raise -- with no collector current and nothing else `active()` anywhere, there is
    # simply nothing to distrust.
    collector.record_first_party(_sample.__code__)
    assert collector.current_collector.get() is None


def test_collector_starts_trusted() -> None:
    assert collector.Collector().untrusted is None


def test_mark_untrusted_records_the_reason() -> None:
    c = collector.Collector()
    c.mark_untrusted("started a subprocess")
    assert c.untrusted == "started a subprocess"


def test_mark_untrusted_keeps_the_first_reason() -> None:
    c = collector.Collector()
    c.mark_untrusted("first reason")
    c.mark_untrusted("second reason")
    assert c.untrusted == "first reason"


def test_record_first_party_marks_an_in_flight_collector_untrusted_with_no_current_collector() -> (
    None
):
    """The scenario the plan describes: code runs on a thread with no collector of its own while
    a test's collector is still `active()` elsewhere -- that test gets marked untrusted rather
    than the code being silently dropped."""
    c = collector.Collector()
    entered = threading.Event()
    release = threading.Event()

    def hold_active() -> None:
        with collector.active(c):
            entered.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_active)
    holder.start()
    try:
        assert entered.wait(timeout=5)
        # This thread's own current_collector is unrelated to c -- exactly the "unattributed
        # thread" case, simulated without needing affected_trace_threads to be off by anything
        # more than the fact that nothing here copied c's context across.
        assert collector.current_collector.get() is None
        collector.record_first_party(_sample.__code__)
    finally:
        release.set()
        holder.join(timeout=5)
    assert c.untrusted is not None
    assert "_sample" in c.untrusted


def test_record_first_party_leaves_a_collector_untrusted_once_marked() -> None:
    """A collector no longer `active()` by the time it's read back keeps whatever mark it
    picked up while it was -- `active()`'s bookkeeping only tracks who to mark, not how long the
    mark lasts."""
    c = collector.Collector()
    entered = threading.Event()
    release = threading.Event()

    def hold_active() -> None:
        with collector.active(c):
            entered.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_active)
    holder.start()
    entered.wait(timeout=5)
    collector.record_first_party(_sample.__code__)
    release.set()
    holder.join(timeout=5)

    assert c.untrusted is not None


def test_active_no_longer_counts_as_in_flight_once_the_block_exits() -> None:
    c = collector.Collector()
    with collector.active(c):
        pass
    # Reset current_collector to simulate an unattributed thread, the way the tests above do,
    # but this time after `active(c)` has already exited -- c must no longer be reachable.
    collector.record_first_party(_sample.__code__)
    assert c.untrusted is None
