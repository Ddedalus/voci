"""`voci._affected.tracing.traced`: a `Tracer`'s lifetime as a context manager, now also covering
M4's audit hook and `os.environ` recorder (`plans/affected-tests-plan.md`, M3's "a real Tracer
around the parent's own run" bullet, and M4's own)."""

from __future__ import annotations

import os
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


def test_traced_records_a_data_dependency_read_inside_the_block(tmp_path: Path) -> None:
    data = tmp_path / "fixture.json"
    data.write_text("{}")
    collector = _collector.Collector()
    with traced(tmp_path), _collector.active(collector):
        data.read_text()
    assert collector.data_paths == {str(data.resolve())}


def test_traced_records_an_env_var_read_inside_the_block() -> None:
    os.environ["VOCI_TEST_TRACING_VAR"] = "1"
    try:
        collector = _collector.Collector()
        with traced(Path.cwd()), _collector.active(collector):
            _ = os.environ["VOCI_TEST_TRACING_VAR"]
        assert collector.env_names == {"VOCI_TEST_TRACING_VAR"}
    finally:
        del os.environ["VOCI_TEST_TRACING_VAR"]


def test_traced_stops_recording_data_and_env_once_the_block_exits(tmp_path: Path) -> None:
    data = tmp_path / "fixture.json"
    data.write_text("{}")
    os.environ["VOCI_TEST_TRACING_VAR"] = "1"
    try:
        with traced(tmp_path):
            pass
        collector = _collector.Collector()
        with _collector.active(collector):
            data.read_text()
            _ = os.environ["VOCI_TEST_TRACING_VAR"]
        assert collector.data_paths == frozenset()
        assert collector.env_names == frozenset()
    finally:
        del os.environ["VOCI_TEST_TRACING_VAR"]
