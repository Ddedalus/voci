"""Coverage measurement carried across the `@velox.isolated` process boundary.

The parent hands each subprocess a serialized copy of the live coverage configuration, pointed at
data files of its own (`subprocess_env`, used by `isolated.run_isolated`); the subprocess starts
measuring under it before importing the test module (`start_in_subprocess`, called by
`_isolated_worker.main`); the parent merges the data back into its own live measurement once the
subprocess exits (`harvest`). Every entry point is a no-op in a process nothing is measuring,
which velox does not depend on coverage.py to determine -- `Coverage.current()` answers it.

A measurement that can't be made is reported through the run's `note`, never raised or warned:
what velox failed to record says nothing about the test that just ran.

`import coverage` here is the third-party package: imports are absolute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from coverage import Coverage

__all__ = ["harvest", "start_in_subprocess", "subprocess_env"]

#: Serializing a live configuration for a subprocess (`CoverageConfig.serialize`, read back
#: through `COVERAGE_PROCESS_CONFIG`) needs coverage.py 7.10. Below that the isolated tier goes
#: unmeasured, which has to be said out loud: a report is otherwise quietly wrong.
_TOO_OLD = (
    "velox: coverage.py is measuring this run but is too old to extend that measurement into "
    "@velox.isolated subprocesses (7.10 or newer is needed). Lines that only an isolated test "
    "reaches will be reported as unexecuted; every other test is measured as usual."
)


def _active() -> Coverage | None:
    """The `Coverage` instance measuring this process, or `None` -- coverage.py not being
    installed included."""
    try:
        import coverage
    except ImportError:
        return None
    return coverage.Coverage.current()


def subprocess_env(data_file: Path, *, note: Callable[[str], None]) -> dict[str, str] | None:
    """Environment putting a `@velox.isolated` subprocess under this process's own measurement,
    writing under `data_file`. `None` when nothing is measuring this process, or when coverage.py
    is too old to carry its configuration across -- noted per isolated test rather than once, so
    the count of what the gap costs is visible.
    """
    cov = _active()
    if cov is None:
        return None
    if not hasattr(cov.config, "serialize"):
        note(_TOO_OLD)
        return None
    config = cov.config.copy()
    # A copy of the parent's whole configuration, differing only in where it writes: settings that
    # disagree across the boundary make the child's data unmergeable or wrongly scoped
    # (plans/rationale.md). Parallel mode because the subprocess is not necessarily the only one
    # measuring under this environment -- it may spawn processes of its own, and two of them
    # sharing one data file would race their saves against each other. `harvest` reads back
    # whatever set of suffixed files that leaves.
    config.data_file = str(data_file)
    config.parallel = True
    return {"COVERAGE_PROCESS_CONFIG": config.serialize()}


def start_in_subprocess() -> None:
    """Start measuring this subprocess, if the environment `subprocess_env` builds asks for it.

    Call before importing the test module: a module already in `sys.modules` when measurement
    starts has no line data left to give.
    """
    try:
        import coverage
    except ImportError:
        return
    # Idempotent by design, which this relies on twice over: coverage.py's own site hook calls it
    # at interpreter startup where site processing runs, and it registers the `atexit` save that
    # writes the data files the parent goes on to harvest.
    coverage.process_startup()


def harvest(data_file: Path, *, note: Callable[[str], None]) -> None:
    """Merge what a finished subprocess measured into this process's live measurement.

    `data_file` is the path handed to `subprocess_env`; what a subprocess and anything it spawned
    actually wrote is that name plus coverage.py's own per-process suffixes, and every one of
    them is merged. Nothing written at all is silence rather than an error -- what an unmeasured
    subprocess, or one that died before its save, leaves behind.
    """
    cov = _active()
    if cov is None or not data_file.parent.is_dir():
        return
    import coverage

    # coverage.py appends a per-process suffix to the name it was handed, so a subprocess's data
    # is everything starting with it: one file usually, more when the test spawned processes.
    written = [p for p in sorted(data_file.parent.iterdir()) if p.name.startswith(data_file.name)]
    if not written:
        return
    # Once, not per file: `get_data` flushes the collector and walks the source tree behind it.
    parent_data = cov.get_data()
    for path in written:
        child = coverage.CoverageData(basename=str(path))
        try:
            child.read()
            parent_data.update(child)
        except Exception as exc:
            # Broad because coverage.py's failures here are its own hierarchy over sqlite3's,
            # and a note because none of them says anything about the test that just ran:
            # failing the run over an unreadable data file would report a passing test as broken.
            note(f"velox: could not merge coverage data from {path}: {exc}")
