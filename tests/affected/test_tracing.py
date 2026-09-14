"""`voci._affected.tracing.traced`: a `Tracer`'s lifetime as a context manager
(`plans/affected-tests-plan.md`, M3's still-missing "a real Tracer around the parent's own run"
bullet)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from voci._affected import collector as _collector
from voci._affected.tracer import Tracer
from voci._affected.tracing import traced


def test_traced_records_first_party_code_that_runs_inside_the_block(tmp_path: Path) -> None:
    module = tmp_path / "mod.py"
    module.write_text("def handler():\n    return 1\n")
    namespace: dict[str, object] = {}
    code = compile(module.read_text(), str(module), "exec")

    collector = _collector.Collector()
    with traced(tmp_path) as reason:
        assert reason is None
        with _collector.active(collector):
            exec(code, namespace)
            handler = cast("Callable[[], int]", namespace["handler"])
            handler()

    qualnames = {qualname for _filename, qualname in collector.finish().codes}
    assert "handler" in qualnames


def test_traced_always_stops_the_tracer_even_on_an_exception(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="boom"), traced(tmp_path):
        raise ValueError("boom")

    # The tool id traced() claimed is free again -- a fresh Tracer can claim it, proving stop()
    # ran on the way out despite the exception.
    tracer = Tracer(tmp_path, _collector.record_first_party)
    try:
        assert tracer.start() is None
    finally:
        tracer.stop()


def test_traced_yields_a_reason_when_no_tool_id_is_free(tmp_path: Path) -> None:
    outer = Tracer(tmp_path, _collector.record_first_party)
    other = Tracer(tmp_path, _collector.record_first_party)
    assert outer.start() is None
    assert other.start() is None
    try:
        with traced(tmp_path) as reason:
            assert reason is not None
    finally:
        other.stop()
        outer.stop()
