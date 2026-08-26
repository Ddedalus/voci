"""Tests for velox_migrate.verify: reading each runner back, comparing the two, and the
renderings of what they disagree about.

One test runs both runners for real, against a pair of hand-written trees, because that is the
only thing that checks the two halves against the output the runners actually produce rather than
against a sample of it pasted into this file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from velox_migrate import cli, outcomes, verify
from velox_migrate.verify import report as verify_report
from velox_migrate.verify.runners import Run

BEFORE = """
import pytest


@pytest.fixture
def value():
    return 3


def test_passes(value):
    assert value == 3


@pytest.mark.parametrize("case", ["a", "b b"])
def test_cases(case):
    assert case


@pytest.mark.skip(reason="not yet")
def test_skipped():
    raise AssertionError


def test_runtime_skip():
    pytest.skip("nope")


@pytest.mark.xfail(reason="known")
def test_xfails():
    raise AssertionError


def test_diverges():
    assert True
"""

AFTER = """
import velox


@velox.fixture()
def value() -> int:
    return 3


def test_passes(value: int = velox.Depends(value)):
    assert value == 3


@velox.parametrize("case", ["a", "b b"])
def test_cases(case):
    assert case


@velox.skip(reason="not yet")
def test_skipped():
    raise AssertionError


def test_runtime_skip():
    raise velox.Skipped("nope")


@velox.xfail(reason="known")
def test_xfails():
    raise AssertionError


def test_diverges():
    raise AssertionError
"""


def run(
    runner: str, outcomes_by_id: dict[str, str], collection_errors: tuple[str, ...] = ()
) -> Run:
    return Run(
        runner=runner,
        tree=Path("."),
        outcomes=outcomes_by_id,
        collection_errors=collection_errors,
    )


def test_the_two_defaults_the_plugin_and_the_runners_name_are_the_same() -> None:
    # The plugin cannot be imported from the runners — it imports pytest — so the constants
    # exist in both on purpose, exactly as `extract`'s do.
    assert verify.DEFAULT_BASELINE == outcomes.DEFAULT_OUT
    assert verify.runners.OUTCOMES_VERSION == outcomes.OUTCOMES_VERSION


def test_two_runs_that_agree_have_nothing_to_review() -> None:
    before = run("pytest", {"t.py::a": "passed", "t.py::b": "skipped"})
    after = run("velox", {"t.py::a": "passed", "t.py::b": "skipped"})

    verification = verify.compare(before, after)

    assert verification.ok
    assert verification.agreed == 2
    assert verification.divergences == ()


def test_each_kind_of_disagreement_is_named_and_ordered_worst_first() -> None:
    before = run("pytest", {"t.py::gone": "passed", "t.py::moved": "passed"})
    after = run("velox", {"t.py::moved": "failed", "t.py::new": "passed"})

    verification = verify.compare(before, after)

    assert [(item.kind, item.id) for item in verification.divergences] == [
        ("missing", "t.py::gone"),
        ("unexpected", "t.py::new"),
        ("outcome", "t.py::moved"),
    ]
    assert verification.agreed == 0
    assert not verification.ok


def test_a_run_that_could_not_collect_is_not_a_clean_verification() -> None:
    before = run("pytest", {"t.py::a": "passed"})
    after = run("velox", {"t.py::a": "passed"}, collection_errors=("u.py",))

    verification = verify.compare(after=after, before=before)

    assert verification.divergences == ()
    assert not verification.ok
    assert "could not collect u.py" in verify_report.terminal(verification)


def _velox_report(**overrides: object) -> dict:
    base: dict = {
        "report_version": 1,
        "runner": "velox",
        "exit_status": 0,
        "collection_errors": [],
        "tests": [
            {"id": "tests/test_a.py::test_one", "outcome": "passed", "duration": 0.01},
            {
                "id": "tests/test_a.py::test_two[b b]",
                "outcome": "passed",
                "duration": 0.0,
            },
            {
                "id": "tests/test_a.py::test_three",
                "outcome": "cancelled",
                "duration": 0.0,
            },
        ],
    }
    base.update(overrides)
    return base


def test_a_velox_report_is_read_back_test_for_test() -> None:
    run = verify.velox_record(_velox_report(), tree=Path("."))

    assert run.outcomes == {
        "tests/test_a.py::test_one": "passed",
        "tests/test_a.py::test_two[b b]": "passed",
        "tests/test_a.py::test_three": "cancelled",
    }


def test_a_velox_reports_collection_errors_carry_through() -> None:
    run = verify.velox_record(
        _velox_report(collection_errors=["tests/test_a.py"], tests=[]), tree=Path(".")
    )

    assert run.outcomes == {}
    assert run.collection_errors == ("tests/test_a.py",)


def test_a_velox_report_from_another_version_of_the_tool_is_refused(tmp_path: Path) -> None:
    report = tmp_path / "velox-report.json"
    report.write_text(json.dumps(_velox_report(report_version=99)), encoding="utf-8")

    with pytest.raises(verify.RunnerError, match="report version"):
        verify.load_velox_report(report)


def test_a_run_of_a_suite_neither_runner_collected_is_not_a_pass() -> None:
    # What a mistyped path leaves: two runners with nothing to disagree about, and a gate that
    # would otherwise report a whole suite verified.
    verification = verify.compare(run("pytest", {}), run("velox", {}))

    assert not verification.ok


def test_a_baseline_from_another_version_of_the_tool_is_refused(tmp_path: Path) -> None:
    baseline = tmp_path / "pytest-outcomes.json"
    baseline.write_text(json.dumps({"outcomes_version": 99, "tests": {}}), encoding="utf-8")

    with pytest.raises(verify.RunnerError, match="Re-record the baseline"):
        verify.load_record(baseline)


def test_a_missing_baseline_says_how_to_record_one(tmp_path: Path) -> None:
    with pytest.raises(verify.RunnerError, match="--record"):
        verify.load_record(tmp_path / "nothing.json")


def test_a_baseline_from_a_run_that_stopped_short_is_refused(tmp_path: Path) -> None:
    # The record outlives the tree it was taken from, so the run's own verdict has to be checked
    # every time it is read, not only when it was written.
    baseline = tmp_path / "pytest-outcomes.json"
    baseline.write_text(
        json.dumps({"outcomes_version": 1, "exit_status": 2, "tests": {"t.py::a": "passed"}}),
        encoding="utf-8",
    )

    with pytest.raises(verify.RunnerError, match="stopped before running the whole suite"):
        verify.load_record(baseline)


def test_a_pytest_run_that_stopped_short_records_nothing_to_compare_against(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "suite"
    (tree / "tests").mkdir(parents=True)
    (tree / "tests" / "test_stops.py").write_text(
        "def test_one():\n    raise KeyboardInterrupt\n\n\ndef test_two():\n    assert True\n",
        encoding="utf-8",
    )
    baseline = tmp_path / "pytest-outcomes.json"

    with pytest.raises(verify.RunnerError, match="never ran the whole suite"):
        verify.run_pytest(tree, out=baseline, paths=[], extra=[])

    assert not baseline.exists()


def test_narrowing_a_run_the_baseline_stands_in_for_is_refused(tmp_path: Path) -> None:
    with pytest.raises(verify.RunnerError, match="no pytest run to narrow"):
        verify.run(
            before_tree=None,
            after_tree=tmp_path,
            baseline=tmp_path / "pytest-outcomes.json",
            paths=["tests/test_one.py"],
        )


def test_the_report_lists_every_divergence_the_terminal_summary_elides() -> None:
    before = run("pytest", {f"t.py::test_{index}": "passed" for index in range(20)})
    after = run("velox", {f"t.py::test_{index}": "failed" for index in range(20)})

    verification = verify.compare(before, after)
    summary, document = (
        verify_report.terminal(verification),
        verify_report.markdown(verification),
    )

    assert f"... {20 - verify_report.TERMINAL_ROWS} more" in summary
    assert all(f"`t.py::test_{index}`" in document for index in range(20))
    assert verify_report.payload(verification)["divergences"][0]["kind"] == "outcome"


def test_both_runners_are_run_and_compared(tmp_path: Path) -> None:
    before_tree, after_tree = tmp_path / "before", tmp_path / "after"
    # The converted tree diverges on purpose, in both directions a real one can: a test whose
    # verdict moved, and a test that is only there at all on one side.
    extra = AFTER + "\n\ndef test_extra():\n    assert True\n"
    for tree, source in ((before_tree, BEFORE), (after_tree, extra)):
        (tree / "tests").mkdir(parents=True)
        (tree / "tests" / "test_smoke.py").write_text(source, encoding="utf-8")

    verification = verify.run(
        before_tree=before_tree,
        after_tree=after_tree,
        baseline=tmp_path / "pytest-outcomes.json",
    )

    assert verification.before.outcomes == {
        "tests/test_smoke.py::test_passes": "passed",
        "tests/test_smoke.py::test_cases[a]": "passed",
        "tests/test_smoke.py::test_cases[b b]": "passed",
        "tests/test_smoke.py::test_skipped": "skipped",
        "tests/test_smoke.py::test_runtime_skip": "skipped",
        "tests/test_smoke.py::test_xfails": "xfailed",
        "tests/test_smoke.py::test_diverges": "passed",
    }
    assert [(item.kind, item.id) for item in verification.divergences] == [
        ("unexpected", "tests/test_smoke.py::test_extra"),
        ("outcome", "tests/test_smoke.py::test_diverges"),
    ]
    assert verification.agreed == 6


def test_a_recorded_baseline_stands_in_for_a_tree_the_conversion_overwrote(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "suite"
    (tree / "tests").mkdir(parents=True)
    (tree / "tests" / "test_smoke.py").write_text(BEFORE, encoding="utf-8")
    baseline = tmp_path / "pytest-outcomes.json"

    recorded = verify.run_pytest(tree, out=baseline, paths=[], extra=[])
    # What `convert --write` does to the tree the baseline was recorded from.
    (tree / "tests" / "test_smoke.py").write_text(AFTER, encoding="utf-8")
    verification = verify.run(before_tree=None, after_tree=tree, baseline=baseline)

    assert verification.before.outcomes == recorded.outcomes
    assert [item.id for item in verification.divergences] == ["tests/test_smoke.py::test_diverges"]


def test_the_command_writes_both_artifacts_and_exits_on_the_divergence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before_tree, after_tree = tmp_path / "before", tmp_path / "after"
    for tree, source in ((before_tree, BEFORE), (after_tree, AFTER)):
        (tree / "tests").mkdir(parents=True)
        (tree / "tests" / "test_smoke.py").write_text(source, encoding="utf-8")
    out = tmp_path / "artifacts"

    code = cli.main(
        [
            "verify",
            "--before",
            str(before_tree),
            "--after",
            str(after_tree),
            "--baseline",
            str(tmp_path / "pytest-outcomes.json"),
            "-o",
            str(out),
        ]
    )

    assert code == 1
    assert "test_diverges" in capsys.readouterr().out
    assert (out / cli.VERIFY_REPORT_NAME).is_file()
    assert json.loads((out / cli.VERIFY_NAME).read_text(encoding="utf-8"))["ok"] is False


def test_recording_without_a_tree_to_record_from_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["verify", "--record", "--baseline", str(tmp_path / "out.json")])

    assert code == 1
    assert "--before" in capsys.readouterr().err
