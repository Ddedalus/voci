"""`--lf`/`--ff`: what the previous run's failures do to this run's test set.

`candidate_files` narrows discovery to the files that could hold one, which is what keeps `--lf`
from importing a suite it has no intention of running any of. `select` and `reorder` then work
over the collected records: the first deselects everything that isn't a recorded failure, the
second keeps the whole suite and lifts the failures to the front of it. `settled_paths` and
`vanished` answer the other direction -- which of the previous run's entries this one is entitled
to overwrite (`velox._cache.merge`).

A file the previous run failed to *collect* contributed no ids to record, so every test in it
counts as a recorded failure; where that file is a package `__init__.py`, so does every test in
the tree below it, none of which was collected either.
"""

from __future__ import annotations

from collections.abc import Container, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from velox._cache import LastRun
from velox._collection.collect import CollectionResult, TestRecord, display_path

__all__ = ["Recorded", "candidate_files", "reorder", "select", "settled_paths", "vanished"]


@dataclass(frozen=True, slots=True)
class Recorded:
    """The previous run's failures, in the form this run matches its own paths and ids against."""

    ids: frozenset[str]
    whole_files: frozenset[str]
    """Rootdir-relative paths of files that failed to collect, replayed test by test."""
    trees: tuple[Path, ...]
    """Directories whose package `__init__.py` is one of `whole_files`. Nothing beneath one was
    collected, so the whole tree is replayed rather than the `__init__.py` alone -- which
    discovery never yields as a test file in the first place."""
    paths: frozenset[str]
    """Every rootdir-relative path a recorded failure names, `whole_files` included."""

    @classmethod
    def of(cls, last_run: LastRun) -> Recorded:
        whole_files = frozenset(last_run.error_files)
        return cls(
            ids=frozenset(last_run.failed),
            whole_files=whole_files,
            trees=tuple(
                Path(path).parent for path in whole_files if Path(path).name == "__init__.py"
            ),
            paths=whole_files | {_path_of(test_id) for test_id in last_run.failed},
        )

    def could_hold_one(self, relative: Path) -> bool:
        """Whether a file at `relative` is worth importing for `--lf`'s sake."""
        return str(relative) in self.paths or self._in_tree(relative)

    def names(self, test_id: str, relative: Path) -> bool:
        """Whether one collected test is a recorded failure, by its own id or by sitting in a
        file that never got as far as having one."""
        return test_id in self.ids or str(relative) in self.whole_files or self._in_tree(relative)

    def _in_tree(self, relative: Path) -> bool:
        return any(tree in relative.parents for tree in self.trees)


def candidate_files(files: Iterable[Path], last_run: LastRun, *, rootdir: Path) -> list[Path]:
    """The files in `files` that a recorded failure names, in the order given."""
    recorded = Recorded.of(last_run)
    resolved_rootdir = Path(rootdir).resolve()
    return [path for path in files if recorded.could_hold_one(display_path(path, resolved_rootdir))]


def select(collected: CollectionResult, last_run: LastRun) -> CollectionResult:
    """`collected` with everything that isn't a recorded failure moved to `deselected`.

    Skips go the same way as records: a skip-marked test the previous run never failed on is one
    this run was not asked for, and leaving it counted would report a `--lf` that executed
    nothing as a run that collected something.
    """
    recorded = Recorded.of(last_run)
    records = [record for record in collected.records if recorded.names(record.id, record.path)]
    skipped = [skip for skip in collected.skipped if recorded.names(skip.id, skip.path)]
    dropped = [
        entry.id
        for entry in (*collected.records, *collected.skipped)
        if not recorded.names(entry.id, entry.path)
    ]
    return replace(
        collected,
        records=_reindexed(records),
        skipped=skipped,
        deselected=[*collected.deselected, *dropped],
    )


def reorder(collected: CollectionResult, last_run: LastRun) -> CollectionResult:
    """`collected` with the recorded failures moved to the front, each half otherwise in the
    logical order collection gave it."""
    recorded = Recorded.of(last_run)
    failed = [record for record in collected.records if recorded.names(record.id, record.path)]
    rest = [record for record in collected.records if not recorded.names(record.id, record.path)]
    return replace(collected, records=_reindexed([*failed, *rest]))


def settled_paths(last_run: LastRun, attempted: Iterable[str]) -> set[str]:
    """The rootdir-relative paths this run has an answer for.

    Every file collection was handed, plus a recorded package `__init__.py` beneath which it was
    handed one: importing that package is what collecting anything under it requires, so a
    failure the run did not report again is one the run fixed. Without the second half a broken
    `__init__.py` stays recorded for good, since discovery never yields it as a file of its own.
    """
    answered = set(attempted)
    for path in last_run.error_files:
        package = Path(path)
        if package.name == "__init__.py" and any(
            package.parent in Path(candidate).parents for candidate in answered
        ):
            answered.add(path)
    return answered


def vanished(
    last_run: LastRun, collected: CollectionResult, *, imported: Container[str]
) -> set[str]:
    """The recorded ids whose tests no longer exist.

    An id under a file in `imported` -- one collection read without error, and so knows the whole
    contents of -- that the file did not produce names a test that has been renamed, deleted or
    moved. Nothing will ever run it, so nothing else would take it out of the cache, and a `--lf`
    would go on narrowing to a file it then selects nothing from.
    """
    deselected = set(collected.deselected)
    existing = deselected | {record.id for record in collected.records}
    existing |= {skip.id for skip in collected.skipped}
    # A test excluded before its `@velox.parametrize` cases were built has no per-case ids for a
    # recorded one to match. Only a deselection counts here: a skip settles its own cases, and
    # matching those too would leave them recorded for good.
    prefixes = tuple(prefix for prefix in collected.unexpanded if prefix in deselected)
    return {
        test_id
        for test_id in last_run.failed
        if _path_of(test_id) in imported
        and test_id not in existing
        and not any(test_id.startswith(f"{prefix}[") for prefix in prefixes)
    }


def _path_of(test_id: str) -> str:
    """The rootdir-relative path a test id starts with. A qualname can hold `::` of its own (a
    method on a `Test*` class), so the path is what precedes the first one."""
    return test_id.partition("::")[0]


def _reindexed(records: list[TestRecord]) -> list[TestRecord]:
    """`records` renumbered from zero, so `index` stays the position of a test in the run that
    is actually about to happen."""
    return [replace(record, index=index) for index, record in enumerate(records)]
