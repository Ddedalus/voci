"""Which of the store's tests the current tree lets you skip, at both discovery-narrowing and
per-test granularity.

Mirrors `_collection.lastfailed`'s own two-step shape -- a file-level narrow, `candidate_files`,
then a per-test filter, `select` -- but the criterion differs: `lastfailed` matches a bare id
against a recorded *failure*; this compares each stored test's dependency checksums against the
tree's *current* ones. `decide` answers that per test, once; `Selection` holds every test's answer
so both steps -- and, later, a `--affected-verify` comparison -- read the same decisions rather
than each recomputing them.

Not this module's job, because it isn't this bullet's: computing `current` (a `World` over the
whole first-party tree, fed to `store.checksums`), resolving `env_key`, or storing anything new --
all the session-start/end driver's, M3's other bullet, not built yet.

**A known gap in `candidate_files`'s own narrowing:** a def block's checksum covers what it
*references*, never what sits beside it, so a brand-new top-level test added to a file whose
every previously-recorded test decided `SKIP` changes nothing any of those tests' own dependency
keys cover -- `could_hold_one` would answer `False` for that file, and the new test would never be
imported at all under a real `--affected` run (contrast `decide` and `select`'s own per-test
filter, which are sound on their own: a test the store has never seen decides `RUN` by default,
same as any other new test). Closing this needs a key that changes whenever a file's own top-level
bindings change, which nothing in `resolve.py`/`blocks.py` computes yet. Until it does, this is
exactly the class of gap the plan's own Failure modes table assigns to `--affected-verify`/a CI
full run -- neither of which narrows via `candidate_files` at all, so both still catch it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from voci._affected.resolve import DependencyKey
from voci._affected.store import StoredRecord
from voci._collection.collect import CollectionResult, display_path, path_of_test_id, reindexed

__all__ = ["Decision", "Selection", "candidate_files", "decide", "select"]


class Decision(Enum):
    """What one stored test's history says about whether it needs to run again."""

    RUN = "run"
    SKIP = "skip"


def decide(records: Iterable[StoredRecord], current: Mapping[DependencyKey, bytes]) -> Decision:
    """One test's decision: among `records` -- its stored history, in any order -- the most
    recent one whose checksums all match `current` decides, per the Selection design section
    ("The most recent record whose checksums all match current tree decides"); every other record
    is irrelevant. A pass skips; a failure, an untrusted mark, or no matching record at all runs
    (Decisions: "A pass skips a test only when it's the most recent matching record; failures,
    errors, timeouts, skips, untrusted tests, and new tests always run")."""
    matching = [record for record in records if _matches(record, current)]
    if not matching:
        return Decision.RUN
    latest = max(matching, key=lambda record: record.last_used)
    if latest.untrusted is not None:
        return Decision.RUN
    return Decision.SKIP if latest.outcome == "passed" else Decision.RUN


def _matches(record: StoredRecord, current: Mapping[DependencyKey, bytes]) -> bool:
    """Stale when any key's current checksum differs or the key has gone -- a gone key's
    `current.get` is `None`, never equal to a stored `bytes` checksum."""
    return all(current.get(key) == checksum for key, checksum in record.dep_checksums.items())


@dataclass(frozen=True, slots=True)
class Selection:
    """Every stored test's `Decision` for the tree as it stands now, in the form this run matches
    its own paths and ids against -- `lastfailed.Recorded`'s counterpart."""

    decisions: Mapping[str, Decision]
    _run_paths: frozenset[str]
    """Rootdir-relative paths of a file holding at least one test decided `RUN`."""
    _known_paths: frozenset[str]
    """Rootdir-relative paths of a file holding at least one stored test, whatever it decided."""

    @classmethod
    def of(
        cls,
        records_by_test: Mapping[str, list[StoredRecord]],
        current: Mapping[DependencyKey, bytes],
    ) -> Selection:
        decisions = {
            test_id: decide(records, current) for test_id, records in records_by_test.items()
        }
        run_paths = {
            path_of_test_id(test_id)
            for test_id, decision in decisions.items()
            if decision is Decision.RUN
        }
        known_paths = {path_of_test_id(test_id) for test_id in decisions}
        return cls(
            decisions=decisions,
            _run_paths=frozenset(run_paths),
            _known_paths=frozenset(known_paths),
        )

    def could_hold_one(self, relative: Path) -> bool:
        """Whether a file at `relative` is worth importing: it's unknown to the store -- a new
        file, whose own tests (new or moved) can't be discovered any other way -- or one of its
        recorded tests decided `RUN`. A file every one of whose recorded tests decided `SKIP`
        needs no import at all: nothing it holds runs, so nothing here has anything to gain from
        being collected -- modulo the module docstring's own gap note: a wholly new test added
        beside only-`SKIP` siblings is invisible here."""
        rel = str(relative)
        return rel not in self._known_paths or rel in self._run_paths

    def decision_for(self, test_id: str) -> Decision:
        """`RUN` for a test with no stored decision at all -- a brand new test, always run
        (Decisions: "new tests ... always run")."""
        return self.decisions.get(test_id, Decision.RUN)

    def unaffected_count_among(self, paths: Iterable[Path], *, rootdir: Path) -> int:
        """How many tests under `paths` -- rootdir-relative, `candidate_files`'s own scope --
        this tree confirms unaffected: `Decision.SKIP`, whatever `candidate_files` and `select`
        went on to do with each one. `--affected`'s own summary line and exit code read this
        rather than a count recovered from `select`'s own `CollectionResult.deselected`:
        `candidate_files` narrows a wholly-`SKIP` file out of collection before `select` ever
        runs, so a count built from what `select` actually saw would miss exactly the common
        case -- every test in a file deciding `SKIP` -- and read a confirmed-unaffected run as a
        genuinely empty suite. `paths` keeps this scoped to what this run's own roots/patterns
        discovered, since `decisions` otherwise spans the whole stored environment: a `voci
        --affected tests/subdir` run must not count a `SKIP` decision for a test outside
        `tests/subdir` -- one this run was never going to look at -- as a reason to call a
        genuinely empty selection a confirmed-unaffected one."""
        resolved_rootdir = Path(rootdir).resolve()
        known = {str(display_path(path, resolved_rootdir)) for path in paths}
        return sum(
            1
            for test_id, decision in self.decisions.items()
            if decision is Decision.SKIP and path_of_test_id(test_id) in known
        )


def candidate_files(files: Iterable[Path], selection: Selection, *, rootdir: Path) -> list[Path]:
    """The files in `files` worth importing under `selection`, in the order given --
    `lastfailed.candidate_files`'s own shape, with "recorded failure" replaced by "not confirmed
    unaffected"."""
    resolved_rootdir = Path(rootdir).resolve()
    return [
        path for path in files if selection.could_hold_one(display_path(path, resolved_rootdir))
    ]


def select(collected: CollectionResult, selection: Selection) -> CollectionResult:
    """`collected` with every test `selection` decided `SKIP` moved to `deselected` --
    `lastfailed.select`'s own shape. A test `selection` has no decision for -- new, or excluded
    from the store some other way -- keeps whatever `collected` already made of it, since
    `decision_for` already answers `RUN` for exactly that case."""
    records = [
        record
        for record in collected.records
        if selection.decision_for(record.id) is not Decision.SKIP
    ]
    skipped = [
        skip for skip in collected.skipped if selection.decision_for(skip.id) is not Decision.SKIP
    ]
    dropped = [
        entry.id
        for entry in (*collected.records, *collected.skipped)
        if selection.decision_for(entry.id) is Decision.SKIP
    ]
    return replace(
        collected,
        records=reindexed(records),
        skipped=skipped,
        deselected=[*collected.deselected, *dropped],
    )
