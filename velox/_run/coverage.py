"""Coverage measurement carried across the `@velox.isolated` process boundary.

The parent hands each subprocess a serialized copy of the live coverage configuration, pointed at
a data file of its own (`subprocess_env`, used by `isolated.run_isolated`); the subprocess starts
measuring under it before importing the test module (`start_in_subprocess`, called by
`_isolated_worker.main`); the parent merges the data back into its own live measurement once the
subprocess exits (`harvest`). Every entry point is a no-op in a process nothing is measuring,
which velox does not depend on coverage.py to determine -- `Coverage.current()` answers it.

`import coverage` here is the third-party package: imports are absolute.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from coverage import Coverage

__all__ = ["harvest", "start_in_subprocess", "subprocess_env"]

#: Serializing a live configuration for a subprocess (`CoverageConfig.serialize`, read back
#: through `COVERAGE_PROCESS_CONFIG`) needs coverage.py 7.10. Below that the isolated tier goes
#: unmeasured, which has to be said out loud: a report is otherwise quietly wrong.
_TOO_OLD = (
    "coverage.py is measuring this run but is too old to extend that measurement into "
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


def subprocess_env(data_file: Path) -> dict[str, str] | None:
    """Environment putting a `@velox.isolated` subprocess under this process's own measurement,
    writing to `data_file`. `None` when nothing is measuring this process, or when coverage.py is
    too old to carry its configuration across -- warned about per isolated test rather than once,
    so the warning summary counts every test the gap costs.
    """
    cov = _active()
    if cov is None:
        return None
    config = cov.config.copy()
    if not hasattr(config, "serialize"):
        warnings.warn(_TOO_OLD, RuntimeWarning, stacklevel=2)
        return None
    # A copy of the parent's whole configuration, differing only in where it writes: settings
    # that disagree across the boundary make the child's data unmergeable or wrongly scoped
    # (plans/rationale.md). `parallel` would suffix the path `harvest` reads back.
    config.data_file = str(data_file)
    config.parallel = False
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
    # writes the data file the parent goes on to harvest.
    coverage.process_startup()


def harvest(data_file: Path) -> None:
    """Merge a finished subprocess's data file into this process's live measurement.

    A missing file is silence, not an error: it is what an unmeasured subprocess, or one that
    died before its save, leaves behind.
    """
    cov = _active()
    if cov is None or not data_file.is_file():
        return
    import coverage

    child = coverage.CoverageData(basename=str(data_file))
    try:
        child.read()
        cov.get_data().update(child)
    except Exception as exc:
        # Broad because coverage.py's failures here are its own hierarchy over sqlite3's, and a
        # warning because none of them says anything about the test that just ran: failing the
        # run over an unreadable data file would report a passing test as broken.
        warnings.warn(
            f"could not merge coverage data from the @velox.isolated subprocess that wrote "
            f"{data_file}: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
