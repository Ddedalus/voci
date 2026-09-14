"""`voci._affected.select`: `decide`'s per-test rule, and the `candidate_files`/`select` pair that
apply it at discovery- and collection-time (`plans/affected-tests-plan.md`, M3's "Narrow through
`lastfailed.candidate_files`, then filter per test like `lastfailed.select`" bullet)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from _support import make_record

from voci._affected import select
from voci._affected.resolve import DependencyKey, NameKey
from voci._affected.select import Decision, Selection
from voci._affected.store import StoredRecord
from voci._collection.collect import CollectionResult, Skipped


def _stub() -> None:
    """Stands in for a collected test function; nothing here calls it."""


_KEY = NameKey(Path("/proj/app.py"), "x")
_CURRENT: Mapping[DependencyKey, bytes] = {_KEY: b"\x01"}


def _record(
    *, outcome: str, checksum: bytes, untrusted: str | None = None, last_used: float = 0.0
) -> StoredRecord:
    return StoredRecord(
        outcome=outcome, untrusted=untrusted, last_used=last_used, dep_checksums={_KEY: checksum}
    )


# -- decide -------------------------------------------------------------------------------------


def test_decide_runs_a_test_with_no_stored_records() -> None:
    assert select.decide([], _CURRENT) is Decision.RUN


def test_decide_skips_a_test_whose_matching_record_passed() -> None:
    records = [_record(outcome="passed", checksum=b"\x01")]
    assert select.decide(records, _CURRENT) is Decision.SKIP


def test_decide_runs_a_test_whose_matching_record_failed() -> None:
    records = [_record(outcome="failed", checksum=b"\x01")]
    assert select.decide(records, _CURRENT) is Decision.RUN


def test_decide_runs_a_test_whose_only_record_does_not_match() -> None:
    """The tree changed since that record; nothing here confirms it's still unaffected."""
    records = [_record(outcome="passed", checksum=b"\x99")]
    assert select.decide(records, _CURRENT) is Decision.RUN


def test_decide_runs_a_test_whose_dependency_key_is_gone() -> None:
    """A key `current` no longer names -- the module was removed, or no longer binds that name --
    is exactly as stale as a key whose checksum changed."""
    records = [_record(outcome="passed", checksum=b"\x01")]
    assert select.decide(records, {}) is Decision.RUN


def test_decide_runs_an_untrusted_matching_record() -> None:
    records = [_record(outcome="passed", checksum=b"\x01", untrusted="reason")]
    assert select.decide(records, _CURRENT) is Decision.RUN


def test_decide_prefers_the_most_recent_matching_record_over_an_older_pass() -> None:
    """Two records both match the current tree; the newer one's outcome decides, even though an
    older matching record passed."""
    records = [
        _record(outcome="passed", checksum=b"\x01", last_used=1.0),
        _record(outcome="failed", checksum=b"\x01", last_used=2.0),
    ]
    assert select.decide(records, _CURRENT) is Decision.RUN


def test_decide_prefers_the_most_recent_matching_record_over_an_older_failure() -> None:
    records = [
        _record(outcome="failed", checksum=b"\x01", last_used=1.0),
        _record(outcome="passed", checksum=b"\x01", last_used=2.0),
    ]
    assert select.decide(records, _CURRENT) is Decision.SKIP


def test_decide_ignores_a_non_matching_record_even_if_it_is_the_newest() -> None:
    """Branch switching: an older record matches the tree as it stands now; a newer one, from a
    different branch, doesn't and is irrelevant."""
    records = [
        _record(outcome="passed", checksum=b"\x01", last_used=1.0),
        _record(outcome="failed", checksum=b"\x99", last_used=2.0),
    ]
    assert select.decide(records, _CURRENT) is Decision.SKIP


# -- Selection ------------------------------------------------------------------------------


def test_selection_decides_every_stored_test() -> None:
    records_by_test = {
        "test_a.py::test_x": [_record(outcome="passed", checksum=b"\x01")],
        "test_a.py::test_y": [_record(outcome="failed", checksum=b"\x01")],
    }
    selection = Selection.of(records_by_test, _CURRENT)
    assert selection.decision_for("test_a.py::test_x") is Decision.SKIP
    assert selection.decision_for("test_a.py::test_y") is Decision.RUN


def test_selection_defaults_an_unknown_test_to_run() -> None:
    selection = Selection.of({}, _CURRENT)
    assert selection.decision_for("test_a.py::test_new") is Decision.RUN


def test_selection_could_hold_one_is_false_only_when_every_recorded_test_skips() -> None:
    records_by_test = {
        "test_a.py::test_x": [_record(outcome="passed", checksum=b"\x01")],
        "test_b.py::test_y": [_record(outcome="passed", checksum=b"\x01")],
        "test_b.py::test_z": [_record(outcome="failed", checksum=b"\x01")],
    }
    selection = Selection.of(records_by_test, _CURRENT)
    assert selection.could_hold_one(Path("test_a.py")) is False
    assert selection.could_hold_one(Path("test_b.py")) is True  # test_z still decided RUN


def test_selection_could_hold_one_is_true_for_a_file_the_store_has_never_seen() -> None:
    selection = Selection.of({}, _CURRENT)
    assert selection.could_hold_one(Path("test_new.py")) is True


# -- candidate_files ------------------------------------------------------------------------


def test_candidate_files_drops_a_file_whose_every_recorded_test_skips(tmp_path: Path) -> None:
    for name in ("test_a.py", "test_b.py"):
        (tmp_path / name).write_text("")
    files = [tmp_path / "test_a.py", tmp_path / "test_b.py"]
    selection = Selection.of(
        {"test_a.py::test_x": [_record(outcome="passed", checksum=b"\x01")]}, _CURRENT
    )
    assert select.candidate_files(files, selection, rootdir=tmp_path) == [tmp_path / "test_b.py"]


def test_candidate_files_keeps_a_file_with_a_test_the_store_has_never_seen(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("")
    files = [tmp_path / "test_a.py"]
    selection = Selection.of({}, _CURRENT)
    assert select.candidate_files(files, selection, rootdir=tmp_path) == files


# -- select ---------------------------------------------------------------------------------


def _collected(*qualnames_by_path: tuple[str, str]) -> CollectionResult:
    records = [
        make_record(index, _stub, qualname, path=Path(path))
        for index, (path, qualname) in enumerate(qualnames_by_path)
    ]
    return CollectionResult(records=records, errors=[], skipped=[])


def _skip(path: str, qualname: str) -> Skipped:
    return Skipped(id=f"{path}::{qualname}", reason="later", path=Path(path))


def test_select_deselects_a_test_decided_skip() -> None:
    collected = _collected(("test_a.py", "test_x"), ("test_a.py", "test_y"))
    selection = Selection.of(
        {"test_a.py::test_x": [_record(outcome="passed", checksum=b"\x01")]}, _CURRENT
    )
    selected = select.select(collected, selection)
    assert [record.id for record in selected.records] == ["test_a.py::test_y"]
    assert selected.deselected == ["test_a.py::test_x"]


def test_select_keeps_a_new_test_with_no_stored_decision() -> None:
    collected = _collected(("test_a.py", "test_new"))
    selected = select.select(collected, Selection.of({}, _CURRENT))
    assert [record.id for record in selected.records] == ["test_a.py::test_new"]


def test_select_renumbers_what_it_keeps() -> None:
    collected = _collected(
        ("test_a.py", "test_x"), ("test_a.py", "test_y"), ("test_a.py", "test_z")
    )
    selection = Selection.of(
        {
            "test_a.py::test_x": [_record(outcome="passed", checksum=b"\x01")],
            "test_a.py::test_y": [_record(outcome="passed", checksum=b"\x01")],
        },
        _CURRENT,
    )
    selected = select.select(collected, selection)
    assert [record.index for record in selected.records] == [0]
    assert [record.id for record in selected.records] == ["test_a.py::test_z"]


def test_select_appends_to_deselections_already_made() -> None:
    collected = _collected(("test_a.py", "test_x"))
    collected.deselected.append("test_a.py::test_earlier")
    selection = Selection.of(
        {"test_a.py::test_x": [_record(outcome="passed", checksum=b"\x01")]}, _CURRENT
    )
    selected = select.select(collected, selection)
    assert selected.deselected == ["test_a.py::test_earlier", "test_a.py::test_x"]


def test_select_deselects_a_skip_decided_skip() -> None:
    collected = _collected(("test_a.py", "test_x"))
    collected.skipped.append(_skip("test_a.py", "test_skipped"))
    selection = Selection.of(
        {
            "test_a.py::test_x": [_record(outcome="failed", checksum=b"\x01")],
            "test_a.py::test_skipped": [_record(outcome="passed", checksum=b"\x01")],
        },
        _CURRENT,
    )
    selected = select.select(collected, selection)
    assert selected.skipped == []
    assert selected.deselected == ["test_a.py::test_skipped"]


def test_select_keeps_a_skip_with_no_stored_decision() -> None:
    collected = _collected()
    collected.skipped.append(_skip("test_a.py", "test_x"))
    selected = select.select(collected, Selection.of({}, _CURRENT))
    assert [skip.id for skip in selected.skipped] == ["test_a.py::test_x"]
