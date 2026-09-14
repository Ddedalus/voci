"""`voci._affected.store`: the git-common-dir location, schema, parse cache, checksum
computation and record storage (`plans/affected-tests-plan.md`, M2's "store.py" bullet)."""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Mapping
from pathlib import Path

from voci._affected import store
from voci._affected.resolve import DefKey, DependencyKey, ModuleKey, NameKey, World


def _world(files: Mapping[str, str], root: Path = Path("/proj")) -> tuple[World, dict[str, Path]]:
    paths = {dotted: root / f"{dotted.replace('.', '/')}.py" for dotted in files}
    return World({paths[dotted]: (dotted, source) for dotted, source in files.items()}), paths


def _files(
    world_files: Mapping[str, str], paths: Mapping[str, Path]
) -> dict[Path, tuple[str, str]]:
    return {paths[dotted]: (dotted, source) for dotted, source in world_files.items()}


# -- Location ---------------------------------------------------------------------------------


def test_store_path_falls_back_to_voci_cache_outside_a_git_repo(tmp_path: Path) -> None:
    assert store.store_path(tmp_path) == tmp_path / ".voci_cache" / "affected.sqlite3"


def test_store_path_uses_the_git_common_dir_inside_a_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert store.store_path(tmp_path) == tmp_path / ".git" / "voci" / "affected.sqlite3"


def test_store_path_is_shared_across_worktrees(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=repo, check=True)
    worktree = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", str(worktree), "-b", "other"], cwd=repo, check=True)
    assert store.store_path(repo) == store.store_path(worktree)


# -- Schema -------------------------------------------------------------------------------------


def test_open_store_creates_the_schema(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"env", "record", "record_dep", "dep_set", "parsed"} <= tables
    finally:
        store.close_store(conn)


def test_open_store_rebuilds_on_a_schema_version_mismatch(tmp_path: Path) -> None:
    path = store.store_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = sqlite3.connect(path)
    stale.execute("CREATE TABLE ancient (id INTEGER)")
    stale.execute("PRAGMA user_version = 999999")
    stale.commit()
    stale.close()

    conn = store.open_store(tmp_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "ancient" not in tables
        assert "record" in tables
    finally:
        store.close_store(conn)


def test_open_store_reopens_cleanly_after_a_rebuild(tmp_path: Path) -> None:
    """The rebuild path (`open_store` -> version mismatch -> temp file -> `os.replace`) must
    leave a store a later `open_store` call in the same process, or a fresh one, can open again
    without tripping another rebuild."""
    store.close_store(store.open_store(tmp_path))
    conn = store.open_store(tmp_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == store._SCHEMA_VERSION
    finally:
        store.close_store(conn)


# -- Parse cache ----------------------------------------------------------------------------


def test_parsed_blocks_hits_the_cache_for_identical_content(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        source = "def a():\n    return 1\n"
        first = store.parsed_blocks(conn, source, "a.py")
        second = store.parsed_blocks(conn, source, "b.py")  # different filename, same content
        assert first == second
        (count,) = conn.execute("SELECT COUNT(*) FROM parsed").fetchone()
        assert count == 1
    finally:
        store.close_store(conn)


def test_parsed_blocks_reparses_on_content_change(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        store.parsed_blocks(conn, "def a():\n    return 1\n", "a.py")
        store.parsed_blocks(conn, "def a():\n    return 2\n", "a.py")
        (count,) = conn.execute("SELECT COUNT(*) FROM parsed").fetchone()
        assert count == 2
    finally:
        store.close_store(conn)


def test_parsed_blocks_survive_close_store_and_reopen(tmp_path: Path) -> None:
    """A caller that only ever parses (never `store_record`s) must not lose that work the moment
    the connection closes -- `close_store` is the one place a write is guaranteed to commit."""
    conn = store.open_store(tmp_path)
    store.parsed_blocks(conn, "def a():\n    return 1\n", "a.py")
    store.close_store(conn)

    reopened = store.open_store(tmp_path)
    try:
        (count,) = reopened.execute("SELECT COUNT(*) FROM parsed").fetchone()
        assert count == 1
    finally:
        store.close_store(reopened)


# -- Checksums ------------------------------------------------------------------------------


def test_a_def_checksum_changes_when_its_body_changes(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        world, paths = _world({"app": "def handler():\n    return 1\n"})
        files = _files({"app": "def handler():\n    return 1\n"}, paths)
        key = DefKey(paths["app"], "handler")
        before = store.checksums(conn, world, files, [key], rootdir=Path("/proj"))[key]

        world2, paths2 = _world({"app": "def handler():\n    return 2\n"})
        files2 = _files({"app": "def handler():\n    return 2\n"}, paths2)
        after = store.checksums(conn, world2, files2, [key], rootdir=Path("/proj"))[key]

        assert before != after
    finally:
        store.close_store(conn)


def test_a_name_checksum_includes_an_effect_folded_from_another_file(tmp_path: Path) -> None:
    with_route = {
        "app": "app = Registry()\n",
        "routes": "from app import app\napp.include_router(1)\n",
    }
    without_route = {"app": "app = Registry()\n", "routes": "from app import app\n"}

    conn = store.open_store(tmp_path)
    try:
        world, paths = _world(with_route)
        checksum_with_route = store.checksums(
            conn,
            world,
            _files(with_route, paths),
            [NameKey(paths["app"], "app")],
            rootdir=Path("/proj"),
        )[NameKey(paths["app"], "app")]

        world2, paths2 = _world(without_route)
        checksum_without_route = store.checksums(
            conn,
            world2,
            _files(without_route, paths2),
            [NameKey(paths2["app"], "app")],
            rootdir=Path("/proj"),
        )[NameKey(paths2["app"], "app")]
    finally:
        store.close_store(conn)

    assert checksum_with_route != checksum_without_route


def test_module_checksum_marks_a_missing_module_absent() -> None:
    checksum = store.module_checksum(
        "definitely_not_a_real_package_xyz", first_party={}, rootdir=Path("/proj")
    )
    assert checksum == store.module_checksum(
        "also_not_a_real_package_abc", first_party={}, rootdir=Path("/proj")
    )


def test_module_checksum_marks_a_dotted_name_with_a_missing_parent_absent() -> None:
    """`find_spec` raises `ModuleNotFoundError` rather than returning `None` when a dotted name's
    *parent* package isn't installed -- the case a `try: import optional_pkg.extra except
    ImportError` for an uninstalled optional dependency hits."""
    checksum = store.module_checksum(
        "definitely_not_a_real_package_xyz.submodule", first_party={}, rootdir=Path("/proj")
    )
    assert checksum == store.module_checksum(
        "definitely_not_a_real_package_xyz", first_party={}, rootdir=Path("/proj")
    )


def test_module_checksum_uses_the_first_party_path_when_known() -> None:
    path = Path("/proj/app.py")
    checksum = store.module_checksum("app", first_party={"app": path}, rootdir=Path("/proj"))
    other_path_checksum = store.module_checksum(
        "app", first_party={"app": Path("/proj/other.py")}, rootdir=Path("/proj")
    )
    assert checksum != other_path_checksum


def test_module_checksum_reflects_an_installed_distributions_version() -> None:
    checksum = store.module_checksum("pytest", first_party={}, rootdir=Path("/proj"))
    absent = store.module_checksum(
        "definitely_not_a_real_package_xyz", first_party={}, rootdir=Path("/proj")
    )
    assert checksum != absent
    # Idempotent: resolving the same distribution twice gives the same answer.
    assert checksum == store.module_checksum("pytest", first_party={}, rootdir=Path("/proj"))


# -- Storing a record -------------------------------------------------------------------------


def test_store_record_keeps_only_the_most_recent_records_per_test(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        for i in range(store._RECORDS_PER_TEST + 4):
            store.store_record(
                conn,
                env_key="env",
                test_id="tests/test_a.py::test_x",
                outcome="passed",
                untrusted=None,
                dep_checksums={NameKey(Path("/proj/app.py"), "x"): bytes([i % 256])},
                rootdir=Path("/proj"),
                now=float(i),
            )
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM record WHERE test_id = ?", ("tests/test_a.py::test_x",)
        ).fetchone()
        assert count == store._RECORDS_PER_TEST
        # The most recent last_used values survive, not the oldest.
        rows = conn.execute("SELECT last_used FROM record ORDER BY last_used").fetchall()
        assert [r[0] for r in rows] == [float(i) for i in range(4, store._RECORDS_PER_TEST + 4)]
    finally:
        store.close_store(conn)


def test_store_record_prunes_dep_sets_no_surviving_record_references(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        for i in range(store._RECORDS_PER_TEST + 4):
            store.store_record(
                conn,
                env_key="env",
                test_id="tests/test_a.py::test_x",
                outcome="passed",
                untrusted=None,
                dep_checksums={NameKey(Path("/proj/app.py"), "x"): bytes([i])},
                rootdir=Path("/proj"),
                now=float(i),
            )
        (dep_set_count,) = conn.execute("SELECT COUNT(*) FROM dep_set").fetchone()
        assert dep_set_count == store._RECORDS_PER_TEST
    finally:
        store.close_store(conn)


def test_store_record_shares_a_dep_set_across_tests(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        shared: dict[DependencyKey, bytes] = {NameKey(Path("/proj/app.py"), "x"): b"\x01"}
        store.store_record(
            conn,
            env_key="env",
            test_id="tests/test_a.py::test_x",
            outcome="passed",
            untrusted=None,
            dep_checksums=shared,
            rootdir=Path("/proj"),
            now=1.0,
        )
        store.store_record(
            conn,
            env_key="env",
            test_id="tests/test_b.py::test_y",
            outcome="passed",
            untrusted=None,
            dep_checksums=shared,
            rootdir=Path("/proj"),
            now=2.0,
        )
        (dep_set_count,) = conn.execute("SELECT COUNT(*) FROM dep_set").fetchone()
        assert dep_set_count == 1
        (record_dep_count,) = conn.execute("SELECT COUNT(*) FROM record_dep").fetchone()
        assert record_dep_count == 2
    finally:
        store.close_store(conn)


def test_load_records_is_empty_for_an_unknown_environment(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        assert store.load_records(conn, "no-such-env", rootdir=tmp_path) == {}
    finally:
        store.close_store(conn)


def test_load_records_decodes_its_own_write(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        dep_checksums: dict[DependencyKey, bytes] = {
            NameKey(tmp_path / "app.py", "x"): b"\x01",
            ModuleKey("pydantic"): b"\x02",
        }
        store.store_record(
            conn,
            env_key="env",
            test_id="tests/test_a.py::test_x",
            outcome="passed",
            untrusted=None,
            dep_checksums=dep_checksums,
            rootdir=tmp_path,
            now=1.0,
        )
        loaded = store.load_records(conn, "env", rootdir=tmp_path)
        [record] = loaded["tests/test_a.py::test_x"]
        assert record.outcome == "passed"
        assert record.untrusted is None
        assert record.last_used == 1.0
        assert dict(record.dep_checksums) == dep_checksums
    finally:
        store.close_store(conn)


def test_load_records_keeps_several_records_per_test(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        for i, outcome in enumerate(("failed", "passed")):
            store.store_record(
                conn,
                env_key="env",
                test_id="tests/test_a.py::test_x",
                outcome=outcome,
                untrusted=None,
                dep_checksums={NameKey(tmp_path / "app.py", "x"): bytes([i])},
                rootdir=tmp_path,
                now=float(i),
            )
        records = store.load_records(conn, "env", rootdir=tmp_path)["tests/test_a.py::test_x"]
        assert {r.outcome for r in records} == {"failed", "passed"}
    finally:
        store.close_store(conn)


def test_load_records_separates_environments(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        store.store_record(
            conn,
            env_key="env-a",
            test_id="tests/test_a.py::test_x",
            outcome="passed",
            untrusted=None,
            dep_checksums={NameKey(tmp_path / "app.py", "x"): b"\x01"},
            rootdir=tmp_path,
            now=1.0,
        )
        assert store.load_records(conn, "env-b", rootdir=tmp_path) == {}
    finally:
        store.close_store(conn)


def test_store_record_groups_module_keys_apart_from_file_keys(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        store.store_record(
            conn,
            env_key="env",
            test_id="tests/test_a.py::test_x",
            outcome="passed",
            untrusted=None,
            dep_checksums={
                NameKey(Path("/proj/app.py"), "x"): b"\x01",
                ModuleKey("pydantic"): b"\x02",
            },
            rootdir=Path("/proj"),
            now=1.0,
        )
        paths = {row[0] for row in conn.execute("SELECT path FROM dep_set").fetchall()}
        assert paths == {"app.py", "module:pydantic"}
    finally:
        store.close_store(conn)
