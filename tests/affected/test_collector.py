"""Tests for voci._affected.collector: what a Tracer's first-party callback attributes running
code to."""

from __future__ import annotations

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


def test_record_first_party_drops_the_code_with_no_current_collector() -> None:
    # Must not raise -- this is the temporary "drop it" half of the docstring, ahead of the
    # untrusted-marking bullet that replaces it.
    collector.record_first_party(_sample.__code__)
    assert collector.current_collector.get() is None
