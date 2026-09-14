"""`voci._affected.driver`: the session-start/end calls over `store.py`/`select.py`/`seeds.py`
(`plans/affected-tests-plan.md`, M3's "still to come" driver bullet)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from voci._affected import driver, store
from voci._affected.collector import CollectorRecord
from voci._affected.resolve import DefKey, DependencyKey, NameKey, World
from voci._affected.select import Decision, Selection
from voci._affected.store import StoredRecord


def _world(files: Mapping[str, str], root: Path) -> tuple[World, dict[Path, tuple[str, str]]]:
    paths = {dotted: root / f"{dotted.replace('.', '/')}.py" for dotted in files}
    mapping = {paths[dotted]: (dotted, source) for dotted, source in files.items()}
    return World(mapping), mapping


# -- prior_selection ---------------------------------------------------------------------------


def test_prior_selection_reports_full_run_reason_when_env_key_unseen(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        world, files = _world({"app": "x = 1\n"}, tmp_path)
        selection, reason = driver.prior_selection(
            conn, world, files, env_key="env", rootdir=tmp_path
        )
        assert reason == driver.FULL_RUN_NO_MATCHING_ENV
        assert selection.decision_for("tests/test_app.py::test_x") is Decision.RUN
    finally:
        store.close_store(conn)


def test_prior_selection_skips_a_test_whose_stored_checksum_still_matches(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        world, files = _world({"app": "x = 1\n"}, tmp_path)
        app_path = next(iter(files))
        current = store.checksums(conn, world, files, [NameKey(app_path, "x")], rootdir=tmp_path)
        store.store_record(
            conn,
            env_key="env",
            test_id="tests/test_app.py::test_x",
            outcome="passed",
            untrusted=None,
            dep_checksums=current,
            rootdir=tmp_path,
            now=1.0,
        )

        selection, reason = driver.prior_selection(
            conn, world, files, env_key="env", rootdir=tmp_path
        )

        assert reason is None
        assert selection.decision_for("tests/test_app.py::test_x") is Decision.SKIP
    finally:
        store.close_store(conn)


def test_prior_selection_runs_a_test_whose_stored_checksum_no_longer_matches(
    tmp_path: Path,
) -> None:
    conn = store.open_store(tmp_path)
    try:
        world, files = _world({"app": "x = 1\n"}, tmp_path)
        app_path = next(iter(files))
        store.store_record(
            conn,
            env_key="env",
            test_id="tests/test_app.py::test_x",
            outcome="passed",
            untrusted=None,
            dep_checksums={NameKey(app_path, "x"): b"\x00" * 8},  # stale, won't match
            rootdir=tmp_path,
            now=1.0,
        )

        selection, reason = driver.prior_selection(
            conn, world, files, env_key="env", rootdir=tmp_path
        )

        assert reason is None
        assert selection.decision_for("tests/test_app.py::test_x") is Decision.RUN
    finally:
        store.close_store(conn)


# -- record_test --------------------------------------------------------------------------------


def test_record_test_stores_the_traced_closures_checksums(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        world, files = _world(
            {"app": "def handler():\n    return 1\n"},
            tmp_path,
        )
        app_path = next(iter(files))
        collector_record = CollectorRecord(codes=frozenset({(str(app_path), "handler")}))

        driver.record_test(
            conn,
            world,
            files,
            test_id="tests/test_app.py::test_handler",
            collector_record=collector_record,
            outcome="passed",
            env_key="env",
            rootdir=tmp_path,
            now=1.0,
        )

        stored = store.load_records(conn, "env", rootdir=tmp_path)
        assert "tests/test_app.py::test_handler" in stored
        (record,) = stored["tests/test_app.py::test_handler"]
        assert record.outcome == "passed"
        assert record.untrusted is None
        assert DefKey(app_path, "handler") in record.dep_checksums
    finally:
        store.close_store(conn)


def test_record_test_carries_the_untrusted_reason_through(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        world, files = _world({"app": "def handler():\n    return 1\n"}, tmp_path)
        app_path = next(iter(files))
        collector_record = CollectorRecord(
            codes=frozenset({(str(app_path), "handler")}), untrusted="unattributed thread"
        )

        driver.record_test(
            conn,
            world,
            files,
            test_id="tests/test_app.py::test_handler",
            collector_record=collector_record,
            outcome="passed",
            env_key="env",
            rootdir=tmp_path,
            now=1.0,
        )

        stored = store.load_records(conn, "env", rootdir=tmp_path)
        (record,) = stored["tests/test_app.py::test_handler"]
        assert record.untrusted == "unattributed thread"
    finally:
        store.close_store(conn)


def test_record_test_stores_nothing_for_a_cancelled_outcome(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        world, files = _world({"app": "def handler():\n    return 1\n"}, tmp_path)
        app_path = next(iter(files))
        collector_record = CollectorRecord(codes=frozenset({(str(app_path), "handler")}))

        driver.record_test(
            conn,
            world,
            files,
            test_id="tests/test_app.py::test_handler",
            collector_record=collector_record,
            outcome="cancelled",
            env_key="env",
            rootdir=tmp_path,
            now=1.0,
        )

        assert store.load_records(conn, "env", rootdir=tmp_path) == {}
    finally:
        store.close_store(conn)


def test_record_test_stores_nothing_when_a_traced_file_changed_mid_run(tmp_path: Path) -> None:
    conn = store.open_store(tmp_path)
    try:
        world, files = _world({"app": "def handler():\n    return 1\n"}, tmp_path)
        app_path = next(iter(files))
        collector_record = CollectorRecord(codes=frozenset({(str(app_path), "handler")}))

        driver.record_test(
            conn,
            world,
            files,
            test_id="tests/test_app.py::test_handler",
            collector_record=collector_record,
            outcome="passed",
            env_key="env",
            rootdir=tmp_path,
            changed_paths=frozenset({app_path}),
            now=1.0,
        )

        assert store.load_records(conn, "env", rootdir=tmp_path) == {}
    finally:
        store.close_store(conn)


# -- verify_prediction ---------------------------------------------------------------------------

_VERIFY_KEY = NameKey(Path("/proj/app.py"), "x")
_VERIFY_CURRENT: Mapping[DependencyKey, bytes] = {_VERIFY_KEY: b"\x01"}


def _selection_predicting_skip() -> Selection:
    records = {
        "test_a.py::test_x": [
            StoredRecord(
                outcome="passed",
                untrusted=None,
                last_used=0.0,
                dep_checksums={_VERIFY_KEY: b"\x01"},
            )
        ]
    }
    return Selection.of(records, _VERIFY_CURRENT)


def test_verify_prediction_is_none_when_a_predicted_skip_stays_a_pass() -> None:
    selection = _selection_predicting_skip()
    assert driver.verify_prediction(selection, "test_a.py::test_x", "passed") is None


def test_verify_prediction_reports_a_mismatch_when_a_predicted_skip_actually_failed() -> None:
    selection = _selection_predicting_skip()
    reason = driver.verify_prediction(selection, "test_a.py::test_x", "failed")
    assert reason == "predicted a skip (recorded passing) -- this run: FAILED"


def test_verify_prediction_is_none_for_a_test_predicted_to_run() -> None:
    """Nothing to disagree with: --affected never promised to skip a test it predicted RUN for,
    so no outcome of that test can mismatch here -- new tests included."""
    selection = Selection.of({}, _VERIFY_CURRENT)
    assert driver.verify_prediction(selection, "test_a.py::test_new", "failed") is None
    assert driver.verify_prediction(selection, "test_a.py::test_new", "passed") is None
