"""Turns one test's finished `CollectorRecord` into `resolve.World.closure`'s seed keys, at
session end (see `plans/affected-tests-plan.md`, M2's "code -> block resolution at session end"
bullet). The actual `(filename, qualname) -> DefKey | NameKey` mapping is `World.resolve_code`;
this module is the thin per-record driver on top of it, plus the one rule that isn't a per-code
lookup at all: a record naming a file that changed mid-run is dropped outright rather than
resolved against stale qualnames.

`non_code_keys_for` is M4's counterpart for the audit hook's and `os.environ` recorder's own
recordings (`data_paths`/`dir_paths`/`env_names`): unlike a `(filename, qualname)` pair, a data
path, a listed directory or an env var name needs no resolution at all -- there's no static
reference for `World.closure` to follow from an `open()` call the way there is from a def's own
body -- so these become `DataKey`/`DirKey`/`EnvKey`s directly, to merge into a test's dependency
set alongside (not through) `World.closure`'s own output.

Turning the returned seeds into a stored, checksummed dependency set -- calling `World.closure`
over them, keying by test, pruning by LRU -- is `store.py`'s job, not built yet.
"""

from __future__ import annotations

from pathlib import Path

from voci._affected.collector import CollectorRecord
from voci._affected.resolve import DataKey, DefKey, DirKey, EnvKey, NameKey, World

__all__ = ["non_code_keys_for", "seeds_for_record"]


def seeds_for_record(
    world: World, record: CollectorRecord, *, changed_paths: frozenset[Path] = frozenset()
) -> frozenset[DefKey | NameKey] | None:
    """The seed keys `record`'s traced `(filename, qualname)` pairs resolve to in `world`, or
    `None` if `record` should be dropped instead of stored: it names a path in `changed_paths`,
    meaning that file was edited *during* the run `record` came from, so the qualnames it recorded
    may no longer correspond to what `world` -- built from the tree as it stands now -- parses
    there at all. This is the same rule `--watch`'s own mid-run stat guard applies elsewhere (the
    plan's "A change during a run": "its mid-run stat guard drops the records of files that
    moved"), applied here per record rather than per iteration.

    `world` should be built from the tree's *current* source, since that's what the returned
    seeds are checksummed against next -- not, say, the tree as it stood when `record`'s test
    started, which `resolve_code` has no way to reconstruct from a bare `(filename, qualname)`
    pair regardless.

    An untrusted record (`record.untrusted is not None`) isn't special-cased here: it still
    resolves to seeds like any other, since Selection's own "always selected" rule is what keeps
    an untrusted test running regardless of what its dependencies say, not a reason to skip
    computing them.
    """
    seeds: set[DefKey | NameKey] = set()
    for filename, qualname in record.codes:
        path = Path(filename)
        if path in changed_paths:
            return None
        seeds |= world.resolve_code(path, qualname)
    return frozenset(seeds)


def non_code_keys_for(record: CollectorRecord) -> frozenset[DataKey | DirKey | EnvKey]:
    """`record`'s own `data_paths`/`dir_paths`/`env_names`, turned into the keys `store.checksums`
    can compute a checksum for -- straight across, no resolution and no mid-run drop: unlike a
    def's own recorded qualname, a data path or env var name names exactly the dependency it is,
    and a data file edited mid-run is caught the ordinary way a changed checksum always is, not by
    a special case here (`seeds_for_record`'s own `changed_paths` rule exists only because a
    `(filename, qualname)` pair can silently resolve to the *wrong* block once its file has moved
    out from under it; a bare path or env var name has nothing analogous to resolve)."""
    return frozenset(
        (
            *(DataKey(Path(path)) for path in record.data_paths),
            *(DirKey(Path(path)) for path in record.dir_paths),
            *(EnvKey(name) for name in record.env_names),
        )
    )
