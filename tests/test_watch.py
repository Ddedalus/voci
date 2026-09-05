"""Tests for `velox._watch`: the `--watch` poll loop, driven by a fake `sleep` and a stub
`run_once` instead of real time or a real subprocess -- see each test's own docstring for what
its fake `sleep` is standing in for.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from velox import _watch


def _run_once_recording(
    calls: list[list[str]], *, statuses: Sequence[int] = ()
) -> Callable[..., int]:
    """A `run_once` stub that records every `argv` it was called with and returns `statuses` in
    order, repeating the last one once `statuses` runs out."""

    def run_once(argv: list[str], *, wall_start: float) -> int:
        calls.append(list(argv))
        index = min(len(calls) - 1, len(statuses) - 1) if statuses else 0
        return statuses[index] if statuses else 0

    return run_once


def test_first_run_uses_the_initial_wall_start(tmp_path: Path) -> None:
    """`initial_wall_start` -- `cli.main`'s own `PROCESS_START` in production -- reaches the
    first `run_once` call untouched; only a rerun a change triggers gets a fresh one
    (`time.monotonic()`, not exercised by this stub)."""
    seen: list[float] = []

    def run_once(argv: list[str], *, wall_start: float) -> int:
        seen.append(wall_start)
        return 0

    def sleep(_: float) -> None:
        raise KeyboardInterrupt  # idle, no change ever comes -- stop after the first run.

    status = _watch.run(
        base_argv=["tests"],
        apply_last_failed=True,
        roots=[tmp_path],
        ignore_dirs=frozenset(),
        initial_wall_start=123.5,
        run_once=run_once,
        sleep=sleep,
    )

    assert seen == [123.5]
    assert status == 0


def test_aborted_status_on_the_first_run_stops_without_watching(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def sleep(_: float) -> None:
        raise AssertionError("must not wait for a change after an aborted run")

    status = _watch.run(
        base_argv=["tests"],
        apply_last_failed=True,
        roots=[tmp_path],
        ignore_dirs=frozenset(),
        initial_wall_start=0.0,
        run_once=_run_once_recording(calls, statuses=(2,)),
        sleep=sleep,
    )

    assert status == 2
    assert calls == [["tests"]]


def test_a_change_reruns_with_lf_appended(tmp_path: Path) -> None:
    """`apply_last_failed=True` -- the invocation didn't already pick --lf/--ff for itself --
    so the rerun a change triggers applies it automatically."""
    target = tmp_path / "test_a.py"
    target.write_text("x = 1\n")
    calls: list[list[str]] = []
    ticks = {"n": 0}

    def sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            # A different size, not just different content -- `_scan` stats rather than
            # hashes, and two writes microseconds apart can otherwise land on the same
            # mtime_ns on a coarse filesystem clock.
            target.write_text("x = 22\n")
        else:
            raise KeyboardInterrupt

    status = _watch.run(
        base_argv=["tests"],
        apply_last_failed=True,
        roots=[tmp_path],
        ignore_dirs=frozenset(),
        initial_wall_start=0.0,
        run_once=_run_once_recording(calls),
        sleep=sleep,
    )

    assert status == 0
    assert calls == [["tests"], ["tests", "--lf"]]


def test_apply_last_failed_false_leaves_argv_alone(tmp_path: Path) -> None:
    """The invocation already gave its own `--lf`/`--ff`, so `--watch` doesn't add a second
    one on top."""
    target = tmp_path / "test_a.py"
    target.write_text("x = 1\n")
    calls: list[list[str]] = []
    ticks = {"n": 0}

    def sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            target.write_text("x = 22\n")
        else:
            raise KeyboardInterrupt

    _watch.run(
        base_argv=["tests", "--ff"],
        apply_last_failed=False,
        roots=[tmp_path],
        ignore_dirs=frozenset(),
        initial_wall_start=0.0,
        run_once=_run_once_recording(calls),
        sleep=sleep,
    )

    assert calls == [["tests", "--ff"], ["tests", "--ff"]]


def test_an_aborted_rerun_stops_the_loop(tmp_path: Path) -> None:
    target = tmp_path / "test_a.py"
    target.write_text("x = 1\n")
    calls: list[list[str]] = []

    def sleep(_: float) -> None:
        target.write_text("x" * (len(calls) + 1) + "\n")

    status = _watch.run(
        base_argv=["tests"],
        apply_last_failed=True,
        roots=[tmp_path],
        ignore_dirs=frozenset(),
        initial_wall_start=0.0,
        run_once=_run_once_recording(calls, statuses=(0, 2)),
        sleep=sleep,
    )

    assert status == 2
    assert len(calls) == 2


def test_ctrl_c_while_idle_returns_the_last_status(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def sleep(_: float) -> None:
        raise KeyboardInterrupt

    status = _watch.run(
        base_argv=["tests"],
        apply_last_failed=True,
        roots=[tmp_path],
        ignore_dirs=frozenset(),
        initial_wall_start=0.0,
        run_once=_run_once_recording(calls, statuses=(1,)),
        sleep=sleep,
    )

    assert status == 1
    assert calls == [["tests"]]


def test_a_change_under_an_ignored_directory_does_not_trigger_a_rerun(tmp_path: Path) -> None:
    (tmp_path / "ignored").mkdir()
    ignored_file = tmp_path / "ignored" / "test_a.py"
    ignored_file.write_text("x = 1\n")
    calls: list[list[str]] = []
    ticks = {"n": 0}

    def sleep(_: float) -> None:
        ticks["n"] += 1
        ignored_file.write_text(f"x = {ticks['n']}\n")
        if ticks["n"] >= 3:
            raise KeyboardInterrupt

    status = _watch.run(
        base_argv=["tests"],
        apply_last_failed=True,
        roots=[tmp_path],
        ignore_dirs=frozenset({"ignored"}),
        initial_wall_start=0.0,
        run_once=_run_once_recording(calls),
        sleep=sleep,
    )

    assert status == 0
    assert calls == [["tests"]]  # the loop never saw a change worth rerunning for
