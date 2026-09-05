"""`--lf`/`--ff`: what the previous run's failures do to this run's test set.

`candidate_files` narrows discovery to the files that could hold one, which is what keeps `--lf`
from importing a suite it has no intention of running any of. `select` and `reorder` then work
over the collected records: the first deselects everything that isn't a recorded failure, the
second keeps the whole suite and lifts the failures to the front of it.

A file the previous run failed to *collect* contributed no ids to record, so both treat every
test in it as a recorded failure -- the alternative is the run that would have reported the
broken import quietly leaving it out.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

from velox._cache import LastRun
from velox._collection.collect import CollectionResult, TestRecord, display_path

__all__ = ["candidate_files", "reorder", "select"]


def candidate_files(files: Iterable[Path], last_run: LastRun, *, rootdir: Path) -> list[Path]:
    """The files in `files` that a recorded failure names, in the order given."""
    wanted = _recorded_paths(last_run)
    resolved_rootdir = Path(rootdir).resolve()
    return [path for path in files if str(display_path(path, resolved_rootdir)) in wanted]


def select(collected: CollectionResult, last_run: LastRun) -> CollectionResult:
    """`collected` with everything that isn't a recorded failure moved to `deselected`."""
    recorded = _recorded(last_run)
    kept = [record for record in collected.records if recorded(record)]
    dropped = [record.id for record in collected.records if not recorded(record)]
    return replace(
        collected,
        records=_reindexed(kept),
        deselected=[*collected.deselected, *dropped],
    )


def reorder(collected: CollectionResult, last_run: LastRun) -> CollectionResult:
    """`collected` with the recorded failures moved to the front, each half otherwise in the
    logical order collection gave it."""
    recorded = _recorded(last_run)
    failed = [record for record in collected.records if recorded(record)]
    rest = [record for record in collected.records if not recorded(record)]
    return replace(collected, records=_reindexed([*failed, *rest]))


def _recorded(last_run: LastRun) -> Callable[[TestRecord], bool]:
    """Whether a record is one the previous run left behind, by its own id or by sitting in a
    file that failed to collect."""
    failed = frozenset(last_run.failed)
    error_files = frozenset(last_run.error_files)
    return lambda record: record.id in failed or str(record.path) in error_files


def _recorded_paths(last_run: LastRun) -> frozenset[str]:
    """The rootdir-relative paths a recorded failure names. A test id is `path::qualname` and a
    qualname may itself contain `::`, so the path is what precedes the first one."""
    return frozenset(
        {test_id.partition("::")[0] for test_id in last_run.failed} | set(last_run.error_files)
    )


def _reindexed(records: list[TestRecord]) -> list[TestRecord]:
    """`records` renumbered from zero, so `index` stays the position of a test in the run that
    is actually about to happen."""
    return [replace(record, index=index) for index, record in enumerate(records)]
