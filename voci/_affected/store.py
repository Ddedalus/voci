"""The affected-test store: where dependency checksums live between runs, and the checksum
computation that feeds it (`plans/affected-tests-plan.md`, M2's "Fingerprints and store" -- the
"store.py" bullet, and the Storage design section).

Three things this module owns, kept separate because each has its own cache key:

- **Location and schema.** `store_path` finds one sqlite3 file per repository, shared by every
  worktree (`git rev-parse --git-common-dir`), falling back to `rootdir/.voci_cache` when git is
  missing or fails -- the store must never be the reason a run errors. `open_store` creates or
  rebuilds the schema, keyed by `PRAGMA user_version`.
- **The parse cache.** `parsed_blocks` wraps `blocks.parse_blocks`, keyed by content sha256
  rather than path, so a file whose content returns to a version seen on another branch is
  parsed once, ever, no matter how many tests or branch switches touch it since.
- **Checksums.** `resolve.py`'s own module docstring assigns this module the two things it
  deliberately doesn't do itself: "Turning the returned keys and fold targets into checksums, and
  merging several statements under one `(path, name)` key's hash". `Fingerprints` is that merge
  for a `DefKey`/`NameKey` -- every block bound to the key's own file, plus (per the Fingerprints
  design section) every effect statement in the corpus whose `World.effect_fold_target` names it,
  found by scanning every first-party file's blocks once rather than re-deriving it per key.
  `module_checksum` is rule 7's `module:` resolution: a first-party path's own relative location,
  a namespace package's sorted directories, "absent", or -- since voci itself ships with zero
  runtime dependencies (`pyproject.toml`) and so cannot import `packaging` to do this properly --
  a hand-rolled requirement-name parse over `importlib.metadata`'s own `Distribution.requires`,
  walking the requirement closure by hand.

What still isn't here, because it isn't this bullet's job: the session-end driver that calls
`seeds.seeds_for_record`, `World.closure` and this module's `checksums` in sequence for every
finished test and hands the result to `store_record` (M3's CLI wiring), and the environment-key
computation whose string `store_record` takes as an opaque `env_key` (M4, plus the Environment
key design section).

`load_records` is `store_record`'s read side, added for M3's "Narrow through
`lastfailed.candidate_files`, then filter per test like `lastfailed.select`" bullet
(`_affected/select.py`): the exact inverse of `_dep_set_ids`/`_group_path`/`_key_id`, decoding a
stored `dep_set` row back into `DependencyKey -> checksum` so `select.decide` can compare it
against the tree's current checksums.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voci._affected.blocks import Block, parse_blocks
from voci._affected.resolve import DefKey, DependencyKey, ModuleKey, NameKey, World

__all__ = [
    "Fingerprints",
    "StoredRecord",
    "build_fingerprints",
    "checksums",
    "close_store",
    "load_records",
    "module_checksum",
    "open_store",
    "parsed_blocks",
    "store_path",
    "store_record",
]

#: Bumped on any change to the tables below. `open_store` deletes and rebuilds the whole file on
#: a mismatch, per the Storage design section -- modeled on testmon's own `check_data_version`,
#: which does the same rather than migrate in place.
_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE env (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    last_used REAL NOT NULL
);

CREATE TABLE record (
    id INTEGER PRIMARY KEY,
    env_id INTEGER NOT NULL REFERENCES env(id),
    test_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    untrusted TEXT,
    last_used REAL NOT NULL
);
CREATE INDEX record_env_test ON record(env_id, test_id);

CREATE TABLE dep_set (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    keyed_checksums TEXT NOT NULL,
    UNIQUE(path, keyed_checksums)
);
CREATE INDEX dep_set_path ON dep_set(path);

CREATE TABLE record_dep (
    record_id INTEGER NOT NULL REFERENCES record(id),
    dep_set_id INTEGER NOT NULL REFERENCES dep_set(id)
);
CREATE INDEX record_dep_record ON record_dep(record_id);
CREATE INDEX record_dep_set ON record_dep(dep_set_id);

CREATE TABLE parsed (
    content_sha TEXT PRIMARY KEY,
    blocks TEXT NOT NULL,
    last_used REAL NOT NULL
);
"""

#: The 8 most recently used records a test keeps, per environment (Selection design section).
_RECORDS_PER_TEST = 8

#: `parsed`'s own cap, so an all-day branch-switching session across a large corpus doesn't grow
#: the store unboundedly with content hashes no branch still names. Exact sizing is M7's job
#: ("Measure size on voci's suite and httpx2 after 20 branch switches"); this is a safety margin,
#: not a tuned figure.
_MAX_PARSED_ENTRIES = 5000


# -- Location and schema ----------------------------------------------------------------------


def store_path(rootdir: Path) -> Path:
    """`<git common dir>/voci/affected.sqlite3`, so every worktree of a repository shares one
    store; `rootdir/.voci_cache/affected.sqlite3` if `rootdir` isn't inside a git repository, or
    git fails, or isn't installed (testmon #214: never error over this)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=rootdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        common_dir = (rootdir / result.stdout.strip()).resolve()
        return common_dir / "voci" / "affected.sqlite3"
    from voci._cache import CACHE_DIR_NAME

    return rootdir / CACHE_DIR_NAME / "affected.sqlite3"


def open_store(rootdir: Path) -> sqlite3.Connection:
    """The store at `store_path(rootdir)`, creating its directory and schema if needed, or
    rebuilding the file whole if its `user_version` doesn't match `_SCHEMA_VERSION`. Callers own
    the returned connection and must `close_store` it -- one write transaction at session end,
    from the parent only, per the Storage design section.

    A rebuild writes the fresh schema into a private temporary file and `os.replace`s it over
    `path`, rather than unlinking `path` and recreating it in place: the store is explicitly
    shared across a repository's worktrees, so two processes racing on the very first run, or
    right after a `_SCHEMA_VERSION` bump, both take this branch at once, and an unlink-then-
    recreate would let the second process delete the file the first just built its schema into.
    `os.replace` is atomic on the platforms voci supports, so whichever process's replace runs
    last simply wins outright -- both temp files hold an equally fresh, empty schema, so there is
    nothing to lose by discarding the loser's."""
    path = store_path(rootdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == _SCHEMA_VERSION:
        return conn
    conn.close()
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp_path.unlink(missing_ok=True)
    temp_conn = _connect(temp_path)
    temp_conn.executescript(_SCHEMA)
    temp_conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    temp_conn.commit()
    temp_conn.close()
    os.replace(temp_path, path)
    return _connect(path)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    # `journal_mode=DELETE`, not the default rollback-journal-or-WAL choice: WAL keeps a second
    # file (`-wal`) that a CI cache copying only the main file loses (testmon #233, #236), and a
    # bare rollback journal already gives one file at rest.
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def close_store(conn: sqlite3.Connection) -> None:
    """Commit whatever this connection hasn't yet, then close it. `sqlite3.Connection.close`
    silently rolls back an uncommitted transaction rather than committing it, so this is the one
    place that write is guaranteed to happen -- a caller that only ever calls `parsed_blocks`
    (never `store_record`, whose own `with conn:` commits eagerly) would otherwise lose every
    parse it just cached the moment the connection closes."""
    conn.commit()
    conn.close()


# -- Parse cache --------------------------------------------------------------------------------


def parsed_blocks(conn: sqlite3.Connection, source: str, filename: str) -> list[Block]:
    """`blocks.parse_blocks(source, filename)`, cached by `source`'s content hash rather than
    `filename` -- a `Block`'s own checksum never depends on the filename it was parsed under
    (`ast.dump` carries no filename), so two files with identical content, or one file returning
    to a version seen on another branch, are parsed once between them."""
    content_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
    now = time.time()
    row = conn.execute("SELECT blocks FROM parsed WHERE content_sha = ?", (content_sha,)).fetchone()
    if row is not None:
        with conn:
            conn.execute(
                "UPDATE parsed SET last_used = ? WHERE content_sha = ?", (now, content_sha)
            )
        return _blocks_from_json(json.loads(row[0]))
    blocks = parse_blocks(source, filename)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO parsed(content_sha, blocks, last_used) VALUES (?, ?, ?)",
            (content_sha, json.dumps(_blocks_to_json(blocks)), now),
        )
        _prune_parsed(conn)
    return blocks


def _blocks_to_json(blocks: Iterable[Block]) -> list[dict[str, Any]]:
    return [
        {
            "qualname": block.qualname,
            "first_line": block.first_line,
            "binds": sorted(block.binds),
            "references": sorted(block.references),
            "effect": block.effect,
            "checksum": block.checksum.hex(),
        }
        for block in blocks
    ]


def _blocks_from_json(data: list[dict[str, Any]]) -> list[Block]:
    return [
        Block(
            qualname=item["qualname"],
            first_line=item["first_line"],
            binds=frozenset(item["binds"]),
            references=frozenset(item["references"]),
            effect=item["effect"],
            checksum=bytes.fromhex(item["checksum"]),
        )
        for item in data
    ]


def _prune_parsed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM parsed WHERE content_sha NOT IN "
        "(SELECT content_sha FROM parsed ORDER BY last_used DESC LIMIT ?)",
        (_MAX_PARSED_ENTRIES,),
    )


# -- Checksums ------------------------------------------------------------------------------


@dataclass(slots=True)
class Fingerprints:
    """Every `DefKey`/`NameKey` checksum reachable from the `World` `build_fingerprints` built
    this over. Built once per session, not once per key: `_folded` is an inverted index over
    every effect statement in the corpus, so `checksum_for` never re-scans it -- the loop
    `resolve.py`'s own module docstring assigns this module ("a key's checksum must call
    `World.effect_fold_target` over every effect statement in the corpus that targets it") runs
    exactly once, here, rather than once per key asked about.
    """

    _own_blocks: dict[Path, list[Block]]
    _folded: dict[DefKey | NameKey, list[bytes]]

    def checksum_for(self, key: DefKey | NameKey) -> bytes:
        """`key`'s checksum: every block in its own file that binds it (its own qualname for a
        `DefKey`, every statement binding its name for a `NameKey`), plus every effect checksum
        folded onto it from anywhere in the corpus."""
        own = [
            block.checksum
            for block in self._own_blocks.get(key.path, ())
            if _block_binds(block, key)
        ]
        return _combine(own + self._folded.get(key, []))


def build_fingerprints(
    conn: sqlite3.Connection, world: World, files: Mapping[Path, tuple[str, str]]
) -> Fingerprints:
    """Parses every file in `files` (the same `{path: (dotted, source)}` mapping `world` was
    built from) through the parse cache, then folds every effect block's `World.effect_fold_target`
    into an inverted index once, up front."""
    own_blocks = {
        path: parsed_blocks(conn, source, str(path)) for path, (_dotted, source) in files.items()
    }
    folded: dict[DefKey | NameKey, list[bytes]] = {}
    for path, blocks in own_blocks.items():
        for block in blocks:
            if not block.effect:
                continue
            for target in world.effect_fold_target(path, block):
                folded.setdefault(target, []).append(block.checksum)
    return Fingerprints(own_blocks, folded)


def _block_binds(block: Block, key: DefKey | NameKey) -> bool:
    if isinstance(key, DefKey):
        return block.qualname == key.qualname
    return key.name in block.binds


def _combine(block_checksums: Iterable[bytes]) -> bytes:
    """One checksum for a key backed by several blocks (redefinitions, an `if`/`else` def, several
    statements binding the same name, several folded effects): sorted so the result doesn't
    depend on scan order, then hashed the same way a single block's own checksum is
    (`blocks.py`'s module docstring: `blake2b-8`)."""
    digest = hashlib.blake2b(digest_size=8)
    for checksum in sorted(block_checksums):
        digest.update(checksum)
    return digest.digest()


_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def module_checksum(dotted: str, *, first_party: Mapping[str, Path], rootdir: Path) -> bytes:
    """Rule 7's `module:<dotted>` checksum -- what `dotted` resolves to right now, not what it
    contains: a first-party path's own rootdir-relative location (content changes are already
    covered by that file's own `DefKey`/`NameKey` checksums), a namespace package's sorted
    directories, "absent", or a third-party distribution's version plus its requirement closure's
    versions (rule 7's own wording), extras included since a requirement string's extras are part
    of what `_REQUIREMENT_NAME_RE` leaves attached to nothing -- they're dropped along with
    version specifiers and markers, which only matters if two different extras of the same
    distribution could pin different transitive versions, a case `importlib.metadata` itself
    can't distinguish either.

    `first_party`: `dotted -> path` for every module in the `World` this checksum is computed
    for, e.g. `{dotted: path for path, (dotted, _source) in files.items()}` over the same mapping
    `World` and `build_fingerprints` were built from.
    """
    if dotted in first_party:
        path = first_party[dotted]
        rel = path.relative_to(rootdir) if path.is_relative_to(rootdir) else path
        return _combine([_string_checksum(str(rel))])
    try:
        spec = importlib.util.find_spec(dotted)
    except ModuleNotFoundError:
        # `find_spec` imports `dotted`'s parent packages to resolve a dotted name (though never
        # `dotted` itself), and raises rather than returning `None` when one of them isn't
        # installed -- unlike a plain top-level absent name, which it reports the ordinary way.
        # A `try: import optional_pkg.extra except ImportError` for an uninstalled optional
        # dependency hits exactly this.
        spec = None
    if spec is not None and spec.origin is None and spec.submodule_search_locations is not None:
        # A namespace package: no `__init__.py`, so no single file's checksum stands for it --
        # rule 7's "a namespace package's directories".
        dirs = sorted(str(location) for location in spec.submodule_search_locations)
        return _combine(_string_checksum(d) for d in dirs)
    top_level = dotted.split(".", 1)[0]
    try:
        distributions = importlib.metadata.packages_distributions().get(top_level, [])
    except Exception:
        distributions = []
    if spec is None and not distributions:
        return _combine([_string_checksum("absent")])
    versions = _requirement_closure_versions(distributions)
    return _combine(_string_checksum(v) for v in versions)


def _requirement_closure_versions(
    distribution_names: Iterable[str], *, _seen: set[str] | None = None
) -> list[str]:
    """`distribution_names`, each one's version, and -- recursively -- every distribution its own
    metadata declares a requirement on: rule 7's "the versions, or absence, of its requirement
    closure". `_seen` guards a requirement cycle, which `importlib.metadata` doesn't itself rule
    out."""
    seen = _seen if _seen is not None else set()
    out: list[str] = []
    for name in distribution_names:
        if name in seen:
            continue
        seen.add(name)
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            out.append(f"{name}:absent")
            continue
        out.append(f"{name}:{distribution.version}")
        for requirement in distribution.requires or ():
            required_name = _requirement_name(requirement)
            if required_name is not None:
                out.extend(_requirement_closure_versions([required_name], _seen=seen))
    return out


def _requirement_name(requirement: str) -> str | None:
    """The project name a `Requires-Dist` line starts with, ignoring the extras, version
    specifiers and environment markers that can follow it -- voci ships with zero runtime
    dependencies (`pyproject.toml`'s own `dependencies = []`), so it cannot import `packaging`'s
    `Requirement` to parse this properly."""
    match = _REQUIREMENT_NAME_RE.match(requirement)
    return match.group(1) if match else None


def _string_checksum(value: str) -> bytes:
    return hashlib.blake2b(value.encode(), digest_size=8).digest()


def checksums(
    conn: sqlite3.Connection,
    world: World,
    files: Mapping[Path, tuple[str, str]],
    keys: Iterable[DependencyKey],
    *,
    rootdir: Path,
    fingerprints: Fingerprints | None = None,
) -> dict[DependencyKey, bytes]:
    """Every one of `keys`' current checksum -- `Fingerprints` for a `DefKey`/`NameKey`,
    `module_checksum` for a `ModuleKey`. `files` is the same mapping `world` was built from.

    `fingerprints`, if given, is used as-is instead of building a fresh one: `build_fingerprints`
    scans every block of every first-party file to invert `World.effect_fold_target` once, so a
    caller making several `checksums` calls against the same `(conn, world, files)` over one run
    -- `driver.record_test`, once per finished test -- builds it once itself and passes it to
    each, rather than paying that whole-corpus scan again per test.
    """
    if fingerprints is None:
        fingerprints = build_fingerprints(conn, world, files)
    first_party = {dotted: path for path, (dotted, _source) in files.items()}
    out: dict[DependencyKey, bytes] = {}
    for key in keys:
        if isinstance(key, ModuleKey):
            out[key] = module_checksum(key.dotted, first_party=first_party, rootdir=rootdir)
        else:
            out[key] = fingerprints.checksum_for(key)
    return out


# -- Storing a record -----------------------------------------------------------------------


def store_record(
    conn: sqlite3.Connection,
    *,
    env_key: str,
    test_id: str,
    outcome: str,
    untrusted: str | None,
    dep_checksums: Mapping[DependencyKey, bytes],
    rootdir: Path,
    now: float | None = None,
) -> None:
    """Record one test's outcome under `dep_checksums`, its resolved dependency closure with each
    key's checksum already computed (`checksums`, above). Keeps only the `_RECORDS_PER_TEST` most
    recently used records for this `(env_key, test_id)` pair, and drops any `dep_set` row no
    surviving record still references -- "Least-recently-used rows are pruned at write time" (the
    Storage design section), done here rather than deferred to a separate sweep, since this is the
    only place new rows are ever added.
    """
    now = time.time() if now is None else now
    with conn:
        env_id = _env_id(conn, env_key, now)
        dep_set_ids = _dep_set_ids(conn, dep_checksums, rootdir)
        cursor = conn.execute(
            "INSERT INTO record(env_id, test_id, outcome, untrusted, last_used) "
            "VALUES (?, ?, ?, ?, ?)",
            (env_id, test_id, outcome, untrusted, now),
        )
        record_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO record_dep(record_id, dep_set_id) VALUES (?, ?)",
            [(record_id, dep_set_id) for dep_set_id in dep_set_ids],
        )
        if _prune_records(conn, env_id, test_id):
            # Only when a record was actually evicted above -- a full anti-join scan of `dep_set`
            # on every call, even the common case where this test hasn't yet reached
            # `_RECORDS_PER_TEST`, would turn N `store_record` calls into O(N * dep_sets) work
            # for no reason: nothing this call did can have orphaned a `dep_set` row otherwise,
            # since `_dep_set_ids` only ever adds references, never removes them.
            _prune_orphan_dep_sets(conn)


def _env_id(conn: sqlite3.Connection, env_key: str, now: float) -> int:
    conn.execute(
        "INSERT INTO env(key, last_used) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET last_used = excluded.last_used",
        (env_key, now),
    )
    row = conn.execute("SELECT id FROM env WHERE key = ?", (env_key,)).fetchone()
    return row[0]


def _dep_set_ids(
    conn: sqlite3.Connection, dep_checksums: Mapping[DependencyKey, bytes], rootdir: Path
) -> list[int]:
    """One `dep_set` row per file `dep_checksums`' keys touch, `UNIQUE(path, keyed_checksums)` so
    an unchanged file's dep set is reused across tests and branches rather than duplicated --
    "shared across tests and records, so a branch variant costs only the dep sets that differ"
    (the Storage design section). A `ModuleKey` has no file of its own, so it gets a synthetic
    one-entry group keyed by its own dotted name -- `module:` keys are already resolved and
    invalidated one at a time (Selection: "Each distinct `module:` key is re-resolved once per
    run"), so nothing needs them grouped with anything else."""
    groups: dict[str, dict[str, str]] = {}
    for key, checksum in dep_checksums.items():
        groups.setdefault(_group_path(key, rootdir), {})[_key_id(key)] = checksum.hex()
    ids: list[int] = []
    for path, keyed in groups.items():
        blob = json.dumps(keyed, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT OR IGNORE INTO dep_set(path, keyed_checksums) VALUES (?, ?)", (path, blob)
        )
        row = conn.execute(
            "SELECT id FROM dep_set WHERE path = ? AND keyed_checksums = ?", (path, blob)
        ).fetchone()
        ids.append(row[0])
    return ids


def _group_path(key: DependencyKey, rootdir: Path) -> str:
    if isinstance(key, ModuleKey):
        return f"module:{key.dotted}"
    return str(key.path.relative_to(rootdir) if key.path.is_relative_to(rootdir) else key.path)


def _key_id(key: DependencyKey) -> str:
    if isinstance(key, DefKey):
        return f"def:{key.qualname}"
    if isinstance(key, NameKey):
        return f"name:{key.name}"
    return "module"


def _prune_records(conn: sqlite3.Connection, env_id: int, test_id: str) -> bool:
    """Delete every record for `(env_id, test_id)` beyond the `_RECORDS_PER_TEST` most recently
    used, and report whether anything was deleted -- so a caller only pays for
    `_prune_orphan_dep_sets`' own full-table scan on the calls that could actually have orphaned
    a `dep_set` row."""
    survivors = (
        "SELECT id FROM record WHERE env_id = ? AND test_id = ? "
        "ORDER BY last_used DESC, id DESC LIMIT ?"
    )
    args = (env_id, test_id, _RECORDS_PER_TEST)
    conn.execute(
        f"DELETE FROM record_dep WHERE record_id IN "
        f"(SELECT id FROM record WHERE env_id = ? AND test_id = ? AND id NOT IN ({survivors}))",
        (env_id, test_id, *args),
    )
    cursor = conn.execute(
        f"DELETE FROM record WHERE env_id = ? AND test_id = ? AND id NOT IN ({survivors})",
        (env_id, test_id, *args),
    )
    return cursor.rowcount > 0


def _prune_orphan_dep_sets(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM dep_set WHERE id NOT IN (SELECT DISTINCT dep_set_id FROM record_dep)")


# -- Reading records back --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredRecord:
    """One stored `record` row, decoded back into the dependency keys `store_record` was given
    for it -- what `select.decide` compares against the tree's current checksums."""

    outcome: str
    untrusted: str | None
    last_used: float
    dep_checksums: Mapping[DependencyKey, bytes]


def load_records(
    conn: sqlite3.Connection, env_key: str, *, rootdir: Path
) -> dict[str, list[StoredRecord]]:
    """Every stored record for `env_key`, keyed by test id. A test id absent from the result has
    no stored record under this environment at all -- a brand new test, or one only ever seen
    under a different `env_key`.

    One joined query for the whole environment, not one round trip per record: a store that has
    accumulated many records (`_RECORDS_PER_TEST` keeps several per test, across every test the
    suite has ever run under this environment) would otherwise make `--affected` pay a `record_dep`
    join per row before a single test runs."""
    row = conn.execute("SELECT id FROM env WHERE key = ?", (env_key,)).fetchone()
    if row is None:
        return {}
    env_id = row[0]
    rows = conn.execute(
        "SELECT record.id, record.test_id, record.outcome, record.untrusted, record.last_used, "
        "dep_set.path, dep_set.keyed_checksums "
        "FROM record "
        "LEFT JOIN record_dep ON record_dep.record_id = record.id "
        "LEFT JOIN dep_set ON dep_set.id = record_dep.dep_set_id "
        "WHERE record.env_id = ? "
        "ORDER BY record.id",
        (env_id,),
    ).fetchall()
    order: list[int] = []
    by_record: dict[int, tuple[str, str, str | None, float, dict[DependencyKey, bytes]]] = {}
    for record_id, test_id, outcome, untrusted, last_used, path, blob in rows:
        if record_id not in by_record:
            order.append(record_id)
            by_record[record_id] = (test_id, outcome, untrusted, last_used, {})
        if path is not None:
            by_record[record_id][4].update(_decode_dep_set(path, json.loads(blob), rootdir))
    out: dict[str, list[StoredRecord]] = {}
    for record_id in order:
        test_id, outcome, untrusted, last_used, dep_checksums = by_record[record_id]
        out.setdefault(test_id, []).append(
            StoredRecord(
                outcome=outcome,
                untrusted=untrusted,
                last_used=last_used,
                dep_checksums=dep_checksums,
            )
        )
    return out


def _decode_dep_set(
    path: str, keyed: Mapping[str, str], rootdir: Path
) -> dict[DependencyKey, bytes]:
    """The inverse of `_group_path`/`_key_id`: `path` is either `module:<dotted>` (one synthetic
    entry, always keyed `"module"`) or a file's own rootdir-relative -- or, for a file outside
    `rootdir`, absolute -- location, holding `def:<qualname>`/`name:<name>` entries."""
    if path.startswith("module:"):
        dotted = path.removeprefix("module:")
        return {ModuleKey(dotted): bytes.fromhex(hexval) for hexval in keyed.values()}
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = rootdir / file_path
    out: dict[DependencyKey, bytes] = {}
    for key_id, hexval in keyed.items():
        if key_id.startswith("def:"):
            out[DefKey(file_path, key_id.removeprefix("def:"))] = bytes.fromhex(hexval)
        elif key_id.startswith("name:"):
            out[NameKey(file_path, key_id.removeprefix("name:"))] = bytes.fromhex(hexval)
    return out
