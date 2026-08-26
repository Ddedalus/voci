"""Running each side of a verification, and reading its outcomes back.

pytest reports through `velox_migrate.outcomes`, which writes a JSON record; velox reports through
its own `--report-json`. Both produce the same `Run`.

Every runner is a subprocess. The two trees are different checkouts of the same suite and each
runner insists on being the one collecting its tree, so nothing here imports either.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# Kept in step with `outcomes.DEFAULT_OUT`/`outcomes.OUTCOMES_VERSION`, which cannot be imported
# from: the plugin imports pytest, and nothing outside `extract`'s subprocess may.
DEFAULT_BASELINE = os.path.join(".velox-migrate", "pytest-outcomes.json")
OUTCOMES_VERSION = 1

# Kept in step with `velox._report.json_report.REPORT_VERSION`, which cannot be imported from
# either: `verify` runs velox as a subprocess in a tree of its own, possibly a different
# environment from the one velox-migrate itself is installed in.
VELOX_REPORT_VERSION = 1

__all__ = [
    "DEFAULT_BASELINE",
    "Run",
    "RunnerError",
    "load_record",
    "load_velox_report",
    "record",
    "run_pytest",
    "run_velox",
    "velox_record",
]

# pytest's own "collection finished" statuses, plus velox's: all passed, something failed, and
# nothing was collected. Anything else — interrupted, internal error, usage error — means the run
# stopped short of reporting on the whole suite, and comparing against it would read every test
# it never reached as a divergence.
CLEAN_EXIT_STATUSES = frozenset({0, 1, 5})


class RunnerError(Exception):
    """A runner could not be run, or ran without reporting on the suite it was pointed at."""


@dataclass(frozen=True)
class Run:
    """One runner's verdict on one tree: an outcome per test id, plus what the run itself did."""

    runner: str
    tree: Path
    outcomes: dict[str, str]
    collection_errors: tuple[str, ...] = ()
    exit_status: int = 0
    duration: float = 0.0
    command: tuple[str, ...] = field(default=(), repr=False)

    def counts(self) -> dict[str, int]:
        """How many tests ended in each outcome, commonest first."""
        tally: dict[str, int] = {}
        for outcome in self.outcomes.values():
            tally[outcome] = tally.get(outcome, 0) + 1
        return dict(sorted(tally.items(), key=lambda pair: (-pair[1], pair[0])))


def run_pytest(tree: Path, *, out: Path, paths: list[str], extra: list[str]) -> Run:
    """Run the pre-migration suite under pytest, recording an outcome per test id.

    `out` is where the plugin writes, and is left on disk: it is the baseline half of the
    comparison, and re-reading it is what lets `verify` run after a conversion has already
    rewritten the tree it was recorded from.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    # A failed run must not leave the previous run's record behind, for the same reason `extract`
    # unlinks the dump: the comparison would read it as this tree's outcomes.
    out.unlink(missing_ok=True)

    command = [
        sys.executable,
        "-m",
        "pytest",
        *paths,
        # Before the passthrough, so a suite that needs its own `--tb` or `-v` gets it.
        "-q",
        "--tb=no",
        *extra,
        "-p",
        "velox_migrate.outcomes",
        "--outcomes-out",
        str(out),
    ]
    completed, duration = _run(command, tree)
    if not out.is_file():
        raise RunnerError(
            f"pytest exited {completed.returncode} in {tree} without recording any outcome:\n"
            f"{_tail(completed)}"
        )
    if completed.returncode not in CLEAN_EXIT_STATUSES:
        # The record exists but describes only the tests pytest reached before it stopped, and
        # every test it never reached would read as one the conversion lost.
        out.unlink(missing_ok=True)
        raise RunnerError(
            f"pytest exited {completed.returncode} in {tree}, so it never ran the whole suite "
            f"and there is nothing to compare against. Fix whatever it reports — the output is "
            f"pytest's own:\n{_tail(completed)}"
        )
    return record(load_record(out), tree=tree, duration=duration, command=command)


def run_velox(tree: Path, *, paths: list[str], concurrency: int) -> Run:
    """Run the converted suite under velox at `concurrency`, reading `--report-json` back.

    The report goes to a scratch file of this call's own — nothing downstream needs it to
    outlive the call, unlike the pytest baseline `run_pytest` leaves on disk.
    """
    executable = _velox_executable()
    if executable is None:
        raise RunnerError(
            "no `velox` command on PATH. `verify` runs both runners, so velox has to be "
            "installed in this environment alongside pytest."
        )
    with tempfile.TemporaryDirectory() as scratch:
        report_path = Path(scratch) / "velox-report.json"
        command = [
            executable,
            *paths,
            "--concurrency",
            str(concurrency),
            "--report-json",
            str(report_path),
        ]
        completed, duration = _run(command, tree)
        if not report_path.is_file():
            raise RunnerError(
                f"velox exited {completed.returncode} in {tree} without writing a report:\n"
                f"{_tail(completed)}"
            )
        if completed.returncode not in CLEAN_EXIT_STATUSES:
            # An interrupted or misconfigured run reports on part of the suite at most, and the
            # part it never reached is indistinguishable from tests the conversion lost.
            raise RunnerError(
                f"velox exited {completed.returncode} in {tree}, so it never ran the whole suite "
                f"and there is nothing to compare:\n{_tail(completed)}"
            )
        loaded = load_velox_report(report_path)
    return velox_record(loaded, tree=tree, duration=duration, command=command)


def load_record(path: Path) -> dict:
    """The JSON `velox_migrate.outcomes` wrote at `path`, refused unless this tool understands
    its shape."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RunnerError(
            f"no baseline at {path}. Record one before converting, with "
            "`velox-migrate verify --record --before <tree>`, or point `--before` at a tree "
            "that still holds the pytest suite."
        ) from None
    except (OSError, ValueError) as exc:
        raise RunnerError(f"{path} is not a readable outcomes record: {exc}") from None
    if not isinstance(loaded, dict) or loaded.get("outcomes_version") != OUTCOMES_VERSION:
        found = loaded.get("outcomes_version") if isinstance(loaded, dict) else None
        raise RunnerError(
            f"{path} is version {found!r}, and this tool writes and reads version "
            f"{OUTCOMES_VERSION}. Re-record the baseline."
        )
    status = loaded.get("exit_status")
    if status not in CLEAN_EXIT_STATUSES:
        # A baseline outlives the tree it was recorded from, so a run that stopped short has to
        # be refused every time it is read rather than only when it was written.
        raise RunnerError(
            f"{path} was recorded from a pytest run that exited {status}, which means it stopped "
            "before running the whole suite. Re-record the baseline."
        )
    return loaded


def record(
    loaded: dict, *, tree: Path, duration: float = 0.0, command: list[str] | None = None
) -> Run:
    """A loaded outcomes record as a `Run`."""
    return Run(
        runner=str(loaded.get("runner", "pytest")),
        tree=tree,
        outcomes={str(key): str(value) for key, value in loaded.get("tests", {}).items()},
        collection_errors=tuple(str(entry) for entry in loaded.get("collection_errors", ())),
        exit_status=int(loaded.get("exit_status", 0)),
        duration=duration,
        command=tuple(command or ()),
    )


def load_velox_report(path: Path) -> dict:
    """The JSON `--report-json` wrote at `path`, refused unless this tool understands its
    shape."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunnerError(f"{path} is not a readable report: {exc}") from None
    if not isinstance(loaded, dict) or loaded.get("report_version") != VELOX_REPORT_VERSION:
        found = loaded.get("report_version") if isinstance(loaded, dict) else None
        raise RunnerError(
            f"{path} is report version {found!r}, and this tool reads version "
            f"{VELOX_REPORT_VERSION}. Is the `velox` on PATH the version `verify` expects?"
        )
    return loaded


def velox_record(
    loaded: dict, *, tree: Path, duration: float = 0.0, command: list[str] | None = None
) -> Run:
    """A loaded `--report-json` record as a `Run`. `verify` only needs the outcome per id --
    `--report-json`'s own duration and failure reason per test are for a consumer with more to
    say about a divergence than `compare` does."""
    return Run(
        runner=str(loaded.get("runner", "velox")),
        tree=tree,
        outcomes={str(entry["id"]): str(entry["outcome"]) for entry in loaded.get("tests", ())},
        collection_errors=tuple(str(entry) for entry in loaded.get("collection_errors", ())),
        exit_status=int(loaded.get("exit_status", 0)),
        duration=duration,
        command=tuple(command or ()),
    )


def _velox_executable() -> str | None:
    """The `velox` command to run, preferring the one installed beside this interpreter.

    `verify` assumes one environment holding both runners, and the environment velox-migrate is
    running in is the one that assumption is about — searching PATH first would find whatever
    velox happens to be on it, which under a `uv run`-style launcher is often nothing at all.
    """
    beside = Path(sys.executable).parent
    return shutil.which("velox", path=os.pathsep.join([str(beside), os.environ.get("PATH", "")]))


def _run(command: list[str], tree: Path) -> tuple[subprocess.CompletedProcess[str], float]:
    if not tree.is_dir():
        raise RunnerError(f"{tree} is not a directory, so there is no suite there to run.")
    environment = dict(os.environ)
    # Neither runner colors a pipe, but a suite's own configuration can force it back on, and an
    # escape sequence in the middle of an id is a divergence that isn't one.
    environment["NO_COLOR"] = "1"
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, cwd=tree, capture_output=True, text=True, env=environment, check=False
        )
    except OSError as exc:
        raise RunnerError(f"could not run {command[0]}: {exc}") from None
    return completed, time.monotonic() - started


def _tail(completed: subprocess.CompletedProcess[str], lines: int = 20) -> str:
    """The last few lines of a runner's own output — what it said about why it stopped."""
    output = (completed.stdout or "") + (completed.stderr or "")
    return "\n".join(output.splitlines()[-lines:])
