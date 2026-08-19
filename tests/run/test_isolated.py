"""Tests for velox._run.isolated: the parent side of a `@velox.isolated` test's subprocess
dispatch. Exercised against a stand-in subprocess (`_FakeProcess`) rather than a real `python -m`
spawn -- `tests/test_cli.py` covers the real thing end to end; these cover `run_isolated`'s own
crash/cancellation handling, which a real subprocess can't be coaxed into on demand.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from _support import make_record as _record
from _support import run_async

from velox._run.isolated import IsolatedConfig, result_to_json, run_isolated
from velox._run.run import Outcome
from velox._run.run import TestResult as Result


async def _passes() -> None:
    pass


class _FakeProcess:
    """Stands in for `asyncio.subprocess.Process`: `communicate()` either returns canned
    stdout/stderr or raises whatever `communicate_error` was given, and `kill`/`wait` just
    record that they were called."""

    def __init__(
        self,
        *,
        returncode: int,
        stderr: bytes = b"",
        communicate_error: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self._stderr = stderr
        self._communicate_error = communicate_error
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._communicate_error is not None:
            raise self._communicate_error
        return b"", self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def _config(rootdir: Path) -> IsolatedConfig:
    return IsolatedConfig(rootdir=rootdir)


def test_result_to_json_round_trips_a_test_result() -> None:
    record = logging.LogRecord(
        name="pkg.mod",
        level=logging.WARNING,
        pathname="x.py",
        lineno=1,
        msg="disk %s full",
        args=("nearly",),
        exc_info=None,
    )
    result = Result(
        id="mod.py::test_thing",
        index=3,
        outcome=Outcome.FAILED,
        duration=0.5,
        failure="Traceback...",
        failure_summary="AssertionError: nope",
        captured_stdout="out",
        captured_stderr="err",
        log_records=(record,),
    )

    data = result_to_json(result)

    assert data == {
        "id": "mod.py::test_thing",
        "index": 3,
        "outcome": "failed",
        "duration": 0.5,
        "failure": "Traceback...",
        "failure_summary": "AssertionError: nope",
        "captured_stdout": "out",
        "captured_stderr": "err",
        "log_records": [{"name": "pkg.mod", "levelname": "WARNING", "message": "disk nearly full"}],
    }


def test_run_isolated_reports_the_worker_written_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = _record(0, _passes, "test_passes")
    written = {
        "id": record.id,
        "index": 0,
        "outcome": "passed",
        "duration": 0.01,
        "failure": None,
        "failure_summary": None,
        "captured_stdout": "",
        "captured_stderr": "",
        "log_records": [],
    }

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        Path(args[4]).write_text(json.dumps(written))
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    data = run_async(
        run_isolated(
            record,
            config=_config(tmp_path),
            timeout=None,
            basetemp_root=tmp_path / "basetemp",
            scratch_dir=tmp_path / "scratch",
        )
    )

    assert data == written


def test_run_isolated_reports_error_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = _record(0, _passes, "test_passes")

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(returncode=1, stderr=b"boom, traceback and all")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    data = run_async(
        run_isolated(
            record,
            config=_config(tmp_path),
            timeout=None,
            basetemp_root=tmp_path / "basetemp",
            scratch_dir=tmp_path / "scratch",
        )
    )

    assert data["id"] == record.id
    assert data["index"] == record.index
    assert data["outcome"] == "error"
    assert "exited with code 1" in data["failure"]
    assert "boom, traceback and all" in data["failure"]


def test_run_isolated_reports_error_when_no_result_file_appears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exit code 0 alone isn't trusted -- a worker that somehow returns without writing
    RESULT_PATH (a bug in the worker, in principle) must still surface as a real result, not
    leave `run_suite`'s `results[index]` slot unfilled."""
    record = _record(0, _passes, "test_passes")

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    data = run_async(
        run_isolated(
            record,
            config=_config(tmp_path),
            timeout=None,
            basetemp_root=tmp_path / "basetemp",
            scratch_dir=tmp_path / "scratch",
        )
    )

    assert data["outcome"] == "error"
    assert "exited with code 0" in data["failure"]


def test_run_isolated_kills_the_subprocess_and_reports_error_on_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Collateral cancellation (a sibling's KeyboardInterrupt/SystemExit propagating through the
    TaskGroup) must not escape `run_isolated` uncaught -- the same contract `_run_one` gives an
    in-process test's own CancelledError branch."""
    record = _record(0, _passes, "test_passes")
    proc = _FakeProcess(returncode=0, communicate_error=asyncio.CancelledError())

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    data = run_async(
        run_isolated(
            record,
            config=_config(tmp_path),
            timeout=None,
            basetemp_root=tmp_path / "basetemp",
            scratch_dir=tmp_path / "scratch",
        )
    )

    assert proc.killed
    assert data["outcome"] == "error"
    assert "cancelled" in data["failure"]


def test_run_isolated_kills_the_subprocess_and_reraises_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = _record(0, _passes, "test_passes")
    proc = _FakeProcess(returncode=0, communicate_error=KeyboardInterrupt())

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(KeyboardInterrupt):
        run_async(
            run_isolated(
                record,
                config=_config(tmp_path),
                timeout=None,
                basetemp_root=tmp_path / "basetemp",
                scratch_dir=tmp_path / "scratch",
            )
        )

    assert proc.killed


def test_run_isolated_hands_the_subprocess_the_runs_own_safety_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A watchdog the user switched off stays off in the subprocess, and a cancelled test
    there gets the same teardown budget as one in the parent."""
    record = _record(0, _passes, "test_passes")
    written_config: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        written_config.update(json.loads(Path(args[3]).read_text()))
        return _FakeProcess(returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    run_async(
        run_isolated(
            record,
            config=_config(tmp_path),
            timeout=2.0,
            basetemp_root=tmp_path / "basetemp",
            scratch_dir=tmp_path / "scratch",
            loop_watchdog=0,
            teardown_grace=1.5,
        )
    )

    assert written_config["loop_watchdog"] == 0
    assert written_config["teardown_grace"] == 1.5
