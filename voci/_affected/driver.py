"""The affected-test session driver: the two calls a real run needs on top of `store.py`/
`select.py`/`seeds.py`/`world.py`'s separate pieces -- a `Selection` before collection,
`store_record` once per finished test after. Not yet wired into `cli.py`; `env_key` is a
placeholder string the caller supplies, pending M4's real computation (`environment.py`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from voci._affected.collector import CollectorRecord
from voci._affected.resolve import DependencyKey, World
from voci._affected.seeds import non_code_keys_for, seeds_for_record
from voci._affected.select import Decision, Selection
from voci._affected.store import Fingerprints, checksums, load_records, store_record

__all__ = ["FULL_RUN_NO_MATCHING_ENV", "prior_selection", "record_test", "verify_prediction"]

#: One of the Selection design section's three full-run reasons -- the other two (a missing or
#: just-rebuilt store, no free `sys.monitoring` tool id) are the caller's own to report, straight
#: from `store.open_store`/`Tracer.start`, since neither needs anything this module computes.
FULL_RUN_NO_MATCHING_ENV = "no stored environment key matches"

#: `decide` never treats these as a skip candidate on their own, but the difference matters here
#: too: a CANCELLED test never got to say anything about the code under test (`run.py`'s own
#: reasoning for why `_settle`'s `vanished` excludes it from what a run may settle), so nothing
#: about it is worth storing at all -- not even an untrusted, always-RUN record.
_NO_RECORD_OUTCOMES = frozenset({"cancelled"})


def prior_selection(
    conn: sqlite3.Connection,
    world: World,
    files: Mapping[Path, tuple[str, str]],
    *,
    env_key: str,
    rootdir: Path,
    fingerprints: Fingerprints | None = None,
    first_party: Mapping[str, Path] | None = None,
) -> tuple[Selection, str | None]:
    """Every stored test's `Decision` against the tree as it stands now, or `(an empty Selection,
    FULL_RUN_NO_MATCHING_ENV)` if `env_key` has no stored records at all -- a first run, or one
    under an environment nothing has run under before.

    `fingerprints`/`first_party`, if given, are passed straight through to `checksums` -- see
    `record_test`'s own docstring for why a caller making several calls over one run wants to
    build both once itself.
    """
    records_by_test = load_records(conn, env_key, rootdir=rootdir)
    if not records_by_test:
        return Selection.of({}, {}), FULL_RUN_NO_MATCHING_ENV
    keys: set[DependencyKey] = set()
    for records in records_by_test.values():
        for record in records:
            keys.update(record.dep_checksums)
    current = checksums(
        conn,
        world,
        files,
        keys,
        rootdir=rootdir,
        fingerprints=fingerprints,
        first_party=first_party,
    )
    return Selection.of(records_by_test, current), None


def verify_prediction(selection: Selection, test_id: str, outcome: str) -> str | None:
    """`--affected-verify`'s own comparison: `None` if `--affected` would have gotten `test_id`
    right (it predicted `RUN`, which every outcome trivially satisfies, or it predicted `SKIP` and
    `outcome` is `"passed"`, confirming the skip would have been safe); otherwise the mismatch
    reason to report, in the docs' own wording ("predicted a skip (recorded passing) -- this run:
    FAILED").

    Only a predicted `SKIP` can ever mismatch: `Selection.decision_for` returning `RUN` is never
    wrong to compare against an outcome, since `--affected` never promised to skip that test in
    the first place -- there is nothing here for `RUN` to disagree with.
    """
    if selection.decision_for(test_id) is not Decision.SKIP:
        return None
    if outcome == "passed":
        return None
    return f"predicted a skip (recorded passing) -- this run: {outcome.upper()}"


def record_test(
    conn: sqlite3.Connection,
    world: World,
    files: Mapping[Path, tuple[str, str]],
    *,
    test_id: str,
    collector_record: CollectorRecord,
    outcome: str,
    env_key: str,
    rootdir: Path,
    changed_paths: frozenset[Path] = frozenset(),
    fingerprints: Fingerprints | None = None,
    first_party: Mapping[str, Path] | None = None,
    now: float | None = None,
) -> None:
    """Store `test_id`'s dependency closure under `collector_record` for this run -- `seeds_for_
    record`, `World.closure` and `store.checksums` in sequence, then `store_record` -- or do
    nothing at all: `outcome` is one `_NO_RECORD_OUTCOMES` excludes, or `seeds_for_record` dropped
    the record outright because one of `collector_record`'s codes names a file `changed_paths`
    says moved mid-run (the same rule `--watch`'s own mid-run stat guard applies elsewhere,
    applied here per test rather than per iteration). `non_code_keys_for` folds in
    `collector_record`'s own `data_paths`/`dir_paths`/`env_names` (M4) alongside `World.closure`'s
    output, not through it -- see that function's own docstring for why a data path or env var
    name needs no resolution first.

    `fingerprints`/`first_party`, if given, are passed straight through to `checksums` instead of
    it building fresh ones: a caller storing one test after another over the same run --
    `cli.py`'s own driver, once per finished test -- builds `store.build_fingerprints(conn,
    world, files)`/`store.first_party_paths(files)` once itself and passes them to every call
    here, rather than paying either's whole-corpus scan again per test.
    """
    if outcome in _NO_RECORD_OUTCOMES:
        return
    seeds = seeds_for_record(world, collector_record, changed_paths=changed_paths)
    if seeds is None:
        return
    closure = world.closure(seeds) | non_code_keys_for(collector_record)
    dep_checksums = checksums(
        conn,
        world,
        files,
        closure,
        rootdir=rootdir,
        fingerprints=fingerprints,
        first_party=first_party,
    )
    store_record(
        conn,
        env_key=env_key,
        test_id=test_id,
        outcome=outcome,
        untrusted=collector_record.untrusted,
        dep_checksums=dep_checksums,
        rootdir=rootdir,
        now=now,
    )
