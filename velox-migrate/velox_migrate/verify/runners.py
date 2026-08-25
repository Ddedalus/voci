"""Running each side of a verification, and reading its outcomes back.

pytest reports through `velox_migrate.outcomes`, which writes a JSON record; velox has no such
plugin seam, so its side is read off `-v`'s own per-test lines. Both produce the same `Run`.

Every runner is a subprocess. The two trees are different checkouts of the same suite and each
runner insists on being the one collecting its tree, so nothing here imports either.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Kept in step with `outcomes.DEFAULT_OUT`/`outcomes.OUTCOMES_VERSION`, which cannot be imported
# from: the plugin imports pytest, and nothing outside `extract`'s subprocess may.
DEFAULT_BASELINE = os.path.join(".velox-migrate", "pytest-outcomes.json")
OUTCOMES_VERSION = 1

__all__ = [
    "DEFAULT_BASELINE",
    "Run",
    "RunnerError",
    "load_record",
    "record",
    "run_pytest",
    "run_velox",
]

# pytest's own "collection finished" statuses, plus velox's: all passed, something failed, and
# nothing was collected. Anything else — interrupted, internal error, usage error — means the run
# stopped short of reporting on the whole suite, and comparing against it would read every test
# it never reached as a divergence.
CLEAN_EXIT_STATUSES = frozenset({0, 1, 5})

#: `-v`'s per-test line: an outcome word, the id, and the duration it ends on. The id column is
#: padded rather than clipped, and an id can hold spaces (a `[case id]` may), so the duration is
#: what bounds it on the right.
_VELOX_LINE = re.compile(r"^([A-Z]+) +(.+?) +\d+\.\d+s$")

#: The header of `-v`'s section listing tests a `skip` mark kept out of the run entirely. They
#: never reach a per-test line, and pytest reports them as skipped, so without this half the
#: suite's skips would read as tests velox never ran.
_VELOX_SKIPPED_HEADER = re.compile(r"^--- skipped \d+ tests? ---$")

_VELOX_COLLECTION_ERROR = re.compile(r"^(.+?) COLLECTION ERROR$")

#: What velox calls its outcomes, lowercased, matching `velox._run.run.Outcome`. Only used to
#: tell a per-test line from a line of test output that happens to look like one.
_VELOX_OUTCOMES = frozenset(
    {"passed", "failed", "error", "skipped", "timeout", "xfailed", "xpassed", "cancelled"}
)


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
        *extra,
        "-p",
        "velox_migrate.outcomes",
        "--outcomes-out",
        str(out),
        "-q",
        "--tb=no",
    ]
    completed, duration = _run(command, tree)
    if not out.is_file():
        raise RunnerError(
            f"pytest exited {completed.returncode} in {tree} without recording any outcome:\n"
            f"{_tail(completed)}"
        )
    return record(load_record(out), tree=tree, duration=duration, command=command)


def run_velox(tree: Path, *, paths: list[str], concurrency: int, extra: list[str]) -> Run:
    """Run the converted suite under velox at `concurrency`, reading `-v`'s lines back."""
    executable = _velox_executable()
    if executable is None:
        raise RunnerError(
            "no `velox` command on PATH. `verify` runs both runners, so velox has to be "
            "installed in this environment alongside pytest."
        )
    command = [executable, *paths, *extra, "--concurrency", str(concurrency), "-v"]
    completed, duration = _run(command, tree)
    outcomes, errors = parse_velox(completed.stdout)
    if not outcomes and completed.returncode not in CLEAN_EXIT_STATUSES:
        raise RunnerError(
            f"velox exited {completed.returncode} in {tree} without running any test:\n"
            f"{_tail(completed)}"
        )
    return Run(
        runner="velox",
        tree=tree,
        outcomes=outcomes,
        collection_errors=errors,
        exit_status=completed.returncode,
        duration=duration,
        command=tuple(command),
    )


def parse_velox(output: str) -> tuple[dict[str, str], tuple[str, ...]]:
    """The outcome per test id, and the files that failed to collect, from a `-v` run's output.

    A test's own captured output is printed too, under the failures it belongs to, so a line is
    only read as a verdict when its first word is an outcome velox actually reports.
    """
    outcomes: dict[str, str] = {}
    errors: list[str] = []
    in_skipped = False
    for line in output.splitlines():
        if _VELOX_SKIPPED_HEADER.match(line):
            in_skipped = True
            continue
        if in_skipped:
            # The section runs to the first blank line; each entry is `id - reason`. An id
            # holding that separator inside a `[case id]` would take the reason with it, which
            # costs a divergence line rather than a wrong verdict.
            if not line.strip():
                in_skipped = False
                continue
            skipped_id = line.split(" - ", 1)[0].strip()
            outcomes.setdefault(skipped_id, "skipped")
            continue
        match = _VELOX_LINE.match(line)
        if match and match.group(1).lower() in _VELOX_OUTCOMES:
            outcomes[match.group(2).strip()] = match.group(1).lower()
            continue
        error = _VELOX_COLLECTION_ERROR.match(line)
        if error:
            errors.append(error.group(1).strip())
    return outcomes, tuple(errors)


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
