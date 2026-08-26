"""Subprocess dispatch for `@velox.isolated`.

`run_isolated` is the parent side: it hands one test's target id and the parent's own
rootdir/assertion-rewrite decision to a fresh `python -m velox._run._isolated_worker`
subprocess, awaits it, and returns the JSON-shaped result dict the worker wrote back --
`run.py`'s `dispatch_one` turns that into a real `TestResult`. Nothing here imports `run.py` at
module level (only under `TYPE_CHECKING`, for annotations): `run.py` imports this module, and
`TestResult`/`Outcome` live there, so a top-level import back would be circular.

Each subprocess gets its own `tmp_path` root, nested under the parent run's own basetemp so it is
swept by the same retention policy, never the parent's root directly -- `_capture.install`'s
explicit-`basetemp` path unconditionally clears whatever already exists there, and the parent's
root is still in use by every other test running concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from velox._builtins.capture import sanitize_test_id

if TYPE_CHECKING:
    from velox._assertions.rewrite import AssertMode
    from velox._collection.collect import TestRecord

__all__ = ["IsolatedConfig", "result_to_json", "run_isolated"]


@dataclass(frozen=True, slots=True)
class IsolatedConfig:
    """What a `@velox.isolated` test's subprocess needs to reproduce the parent run's import
    setup: where `rootdir` is, and the same assertion-rewrite decision the parent already made
    (`cli.main` passes `mode`/`cache_dir` straight from its own `AssertionSetup`, not the raw
    `--assert` flag, so a fallback to `plain` isn't silently re-decided, or re-warned about,
    once per isolated test).
    """

    rootdir: Path
    assert_mode: AssertMode = "rewrite"
    assert_cache_dir: Path | None = None
    rewrite_roots: tuple[Path, ...] = field(default=())


def result_to_json(result: Any) -> dict[str, Any]:
    """A `TestResult` (see `run.py`), as the JSON dict `_isolated_worker` writes to its result
    file. `log_records` keeps only the three fields `_report.terminal` ever reads off a
    `LogRecord` -- `name`, `levelname`, and the already-rendered message -- since the rest of
    `logging.LogRecord` isn't reliably JSON-safe (arbitrary `args`, exception objects) and
    nothing downstream of a `TestResult` needs it.
    """
    return {
        "id": result.id,
        "index": result.index,
        "outcome": result.outcome.value,
        "duration": result.duration,
        "failure": result.failure,
        "failure_summary": result.failure_summary,
        "captured_stdout": result.captured_stdout,
        "captured_stderr": result.captured_stderr,
        "log_records": [
            {"name": r.name, "levelname": r.levelname, "message": r.getMessage()}
            for r in result.log_records
        ],
        "warnings": [asdict(w) for w in result.warnings],
    }


def _crash_result(record: TestRecord, start: float, message: str) -> dict[str, Any]:
    """The same shape `result_to_json` produces, for a subprocess that never wrote a result file
    at all -- crashed, was killed, or produced unreadable output."""
    return {
        "id": record.id,
        "index": record.index,
        "outcome": "error",
        "duration": time.monotonic() - start,
        "failure": message,
        "failure_summary": message.splitlines()[0] if message else "isolated subprocess failed",
        "captured_stdout": "",
        "captured_stderr": "",
        "log_records": [],
    }


async def run_isolated(
    record: TestRecord,
    *,
    config: IsolatedConfig,
    timeout: float | None,
    basetemp_root: Path,
    scratch_dir: Path,
    loop_watchdog: float | None = None,
    teardown_grace: float | None = None,
    filterwarnings: Sequence[str] = (),
) -> dict[str, Any]:
    """Run `record` alone in a fresh subprocess and return its result as a JSON-shaped dict
    (`result_to_json`'s own shape). Always returns -- never raises `asyncio.CancelledError` --
    so `run.py`'s `dispatch_one` can rely on `results[index]` always ending up filled, the same
    guarantee `_run_one` gives an in-process test: a collateral cancellation from a sibling's
    `KeyboardInterrupt`/`SystemExit` kills the subprocess and reports `error` rather than
    propagating, while a genuine `KeyboardInterrupt`/`SystemExit` raised in this coroutine's own
    frame still kills the subprocess but is re-raised, same as `_run_one`'s own setup/call guards.

    `timeout` is this test's own effective budget (suite-wide, or its `@velox.timeout(...)`
    override) -- handed to the subprocess's own `run_suite` call, which enforces it exactly the
    way it would for an in-process test. Nothing here imposes a second, redundant timeout.
    `loop_watchdog`, `teardown_grace` and `filterwarnings` travel the same way, so the subprocess
    runs its one test under the settings the parent run was given rather than the built-in
    defaults.
    """
    start = time.monotonic()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    stem = sanitize_test_id(record.id)
    config_path = scratch_dir / f"{stem}.config.json"
    result_path = scratch_dir / f"{stem}.result.json"
    # Deliberately not created here -- see the module docstring. The worker's own
    # `_capture.install(basetemp=...)` must be the first thing to touch this path.
    child_basetemp = basetemp_root / "isolated" / stem

    config_path.write_text(
        json.dumps(
            {
                "rootdir": str(config.rootdir),
                "file": str(config.rootdir / record.path),
                "target_id": record.id,
                "index": record.index,
                "timeout": timeout,
                "loop_watchdog": loop_watchdog,
                "teardown_grace": teardown_grace,
                "filterwarnings": list(filterwarnings),
                "basetemp": str(child_basetemp),
                "assert_mode": config.assert_mode,
                "assert_cache_dir": (
                    str(config.assert_cache_dir) if config.assert_cache_dir is not None else None
                ),
                "rewrite_roots": [str(root) for root in config.rewrite_roots],
            }
        )
    )

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "velox._run._isolated_worker",
        str(config_path),
        str(result_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        return _crash_result(
            record,
            start,
            "@velox.isolated subprocess cancelled (collateral from a sibling interrupt)",
        )
    except (KeyboardInterrupt, SystemExit):
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        raise

    if proc.returncode == 0 and result_path.is_file():
        try:
            return json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass  # Fall through to the crash report below -- a result file that exists but
            # can't be read back is no more useful than one that was never written.

    detail = stderr.decode(errors="replace").strip()
    message = f"@velox.isolated subprocess exited with code {proc.returncode}"
    if detail:
        message = f"{message}:\n\n{detail}"
    return _crash_result(record, start, message)
