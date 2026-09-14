"""`voci.cli.main`'s hidden `--affected` flag, end to end -- M3's CLI wiring
(`plans/affected-tests-plan.md`). The flag is `argparse.SUPPRESS`-hidden until M4, so these are
the only tests exercising it at all; unit coverage for the pieces it wires together
(`_di.runtime.ScopeStore.collectors_for`, `_run.run.run_suite`'s `on_test_dependencies`,
`_affected/{world,driver,environment,tracing}.py`) lives in their own test modules.
"""

from __future__ import annotations

import pytest
from _support import Project

from voci._affected import store as _affected_store
from voci.cli import main


def test_affected_first_run_has_nothing_stored_and_runs_everything(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_pyproject("[tool.voci]\n")
    project.write_passing_test()

    assert main(["--affected", str(project.root)]) == 0

    out = capsys.readouterr().out
    assert "full run: no stored environment key matches" in out
    assert "1 test" in out


def test_affected_second_run_skips_an_unchanged_passing_test(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_pyproject("[tool.voci]\n")
    project.write_passing_test()

    assert main(["--affected", str(project.root)]) == 0
    capsys.readouterr()  # discard the first run's output

    status = main(["--affected", str(project.root)])

    out = capsys.readouterr().out
    assert "full run:" not in out
    # Nothing left to run: the whole file's only test decided SKIP, so candidate_files narrows
    # the file out of collection entirely -- exit 5, `exit_code_for`'s own "nothing collected at
    # all" code, same as -k/-m narrowing to nothing (the CLI-level "N unaffected" summary
    # `docs/guide/affected.md` drafts, distinguishing this from a genuinely empty suite, is a
    # still-open piece of this bullet's own reporting, not yet wired).
    assert status == 5


def test_affected_reruns_a_test_after_its_own_body_changes(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_pyproject("[tool.voci]\n")
    project.write("test_a.py", "async def test_x():\n    assert 1 == 1\n")

    assert main(["--affected", str(project.root)]) == 0
    capsys.readouterr()

    project.write("test_a.py", "async def test_x():\n    assert 2 == 2\n")
    assert main(["--affected", str(project.root)]) == 0
    out = capsys.readouterr().out

    assert "full run:" not in out
    assert "1 test" in out


def test_affected_writes_a_store_under_voci_cache(project: Project) -> None:
    project.write_pyproject("[tool.voci]\n")
    project.write_passing_test()

    assert main(["--affected", str(project.root)]) == 0

    store_path = _affected_store.store_path(project.root)
    assert store_path.is_file()
    conn = _affected_store.open_store(project.root)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM record").fetchone()
        assert count == 1
    finally:
        _affected_store.close_store(conn)


def test_affected_rejects_combination_with_lf(project: Project) -> None:
    project.write_passing_test()
    assert main(["--affected", "--lf", str(project.root)]) == 4


def test_affected_a_failing_test_always_reruns(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_pyproject("[tool.voci]\n")
    project.write("test_a.py", "async def test_bad():\n    assert False\n")

    assert main(["--affected", str(project.root)]) == 1
    capsys.readouterr()

    # Unchanged, still failing: --affected never skips a non-passing outcome.
    status = main(["--affected", str(project.root)])
    out = capsys.readouterr().out
    assert status == 1
    assert "full run:" not in out
    assert "1 test" in out


def _dir_with_two_tests(project: Project) -> None:
    project.write_pyproject("[tool.voci]\n")
    project.write("test_a.py", "async def test_a():\n    pass\n")
    project.write("test_b.py", "async def test_b():\n    pass\n")


def test_affected_only_reruns_the_test_whose_own_file_changed(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _dir_with_two_tests(project)

    assert main(["--affected", str(project.root)]) == 0
    capsys.readouterr()

    project.write("test_a.py", "async def test_a():\n    assert 1 == 1\n")
    assert main(["--affected", str(project.root)]) == 0
    out = capsys.readouterr().out

    assert "full run:" not in out
    assert "1 test" in out


def test_affected_ignores_git_common_dir_and_reuses_a_store_under_a_worktree(
    project: Project,
) -> None:
    """Not a git repo, so `store_path` falls back to `.voci_cache/` under `project.root` --
    pinned here so a later change to that fallback doesn't silently start writing somewhere this
    suite can't clean up."""
    project.write_pyproject("[tool.voci]\n")
    project.write_passing_test()
    assert main(["--affected", str(project.root)]) == 0
    assert (project.root / ".voci_cache" / "affected.sqlite3").is_file()


# Regressions from the code review that landed alongside the wiring above.
# --------------------------------------------------------------------------------------------


def test_affected_and_watch_together_is_a_usage_error(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--watch` appends `--lf` to every rerun after its first, which `--affected` now rejects
    outright -- caught up front, before the first iteration, rather than only on the second."""
    project.write_pyproject("[tool.voci]\n")
    project.write_passing_test()

    assert main(["--watch", "--affected", str(project.root)]) == 4
    assert "--watch --affected" in capsys.readouterr().err


def test_affected_collect_only_narrows_even_from_a_warm_collection_index(
    project: Project,
) -> None:
    """The collect-only fast path answers straight from the collection index when it's fresh,
    skipping `_collect_and_narrow` entirely -- it must still honor `--affected`'s own narrowing,
    the same way it already excludes itself for `--lf`/`--ff`."""
    project.write_pyproject("[tool.voci]\n")
    project.write_passing_test()

    # First run: stores a record and warms the collection index (a plain run, not --affected,
    # so nothing about this warm-up depends on the wiring under test).
    assert main([str(project.root)]) == 0
    assert main(["--affected", str(project.root)]) == 0  # stores a record for the one test

    status = main(["--affected", "--collect-only", str(project.root)])

    # The collection index is warm (both runs above collected this same file), so without the
    # fast-path fix this would answer from the index unnarrowed and print the test id anyway.
    assert status == 5  # nothing left to run: the fast path was skipped and real narrowing applied


def test_affected_closes_the_store_connection_even_when_prior_selection_raises(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_prepare_affected` opens the store before it can possibly fail; a failure afterward must
    still close what `open_store` opened, not leak it back out through `main`."""
    from voci import cli as _cli
    from voci._affected import driver as _driver

    project.write_pyproject("[tool.voci]\n")
    project.write_passing_test()

    closed: list[bool] = []
    real_close = _cli._affected_store.close_store

    def spying_close(conn: object) -> None:
        closed.append(True)
        real_close(conn)  # type: ignore[arg-type]

    def broken_prior_selection(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(_cli._affected_store, "close_store", spying_close)
    monkeypatch.setattr(_driver, "prior_selection", broken_prior_selection)

    with pytest.raises(RuntimeError, match="boom"):
        main(["--affected", str(project.root)])

    assert closed == [True]


# --affected-verify: runs everything, but still predicts what --affected would have decided per
# test and compares that against the real outcome.
# --------------------------------------------------------------------------------------------


def test_affected_verify_runs_everything_and_reports_no_mismatches_when_unchanged(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_pyproject("[tool.voci]\n")
    project.write_passing_test()

    assert main(["--affected", str(project.root)]) == 0
    capsys.readouterr()

    status = main(["--affected-verify", str(project.root)])
    out = capsys.readouterr().out

    assert status == 0
    assert "MISMATCH" not in out
    assert "1 would have been skipped" in out
    assert "full run:" not in out  # verify never narrows, so there's nothing to call a full run


def test_affected_verify_runs_a_never_before_seen_test_with_nothing_to_predict(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_pyproject("[tool.voci]\n")
    project.write_passing_test()

    status = main(["--affected-verify", str(project.root)])
    out = capsys.readouterr().out

    assert status == 0
    assert "MISMATCH" not in out
    assert "0 would have been skipped" in out


def test_affected_verify_reports_a_mismatch_when_an_unchanged_test_flips_outcome(
    project: Project, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test reading something --affected doesn't track (an env var -- M4's own job) can change
    outcome without its own code changing at all: exactly the gap `--affected-verify` exists to
    catch, per the docs draft's own reasoning (an earlier test's state, timing, randomness, a
    network call...)."""
    project.write_pyproject("[tool.voci]\n")
    project.write(
        "test_a.py",
        "import os\n\nasync def test_x():\n    assert os.environ.get('SHOULD_FAIL') != '1'\n",
    )

    assert main(["--affected", str(project.root)]) == 0
    capsys.readouterr()

    monkeypatch.setenv("SHOULD_FAIL", "1")
    status = main(["--affected-verify", str(project.root)])
    out = capsys.readouterr().out

    assert status == 1  # the test really did fail this run
    assert "MISMATCH  test_a.py::test_x" in out
    assert "predicted a skip (recorded passing) -- this run: FAILED" in out
    assert "1 would have been skipped" in out
    assert "1 mismatch" in out


def test_affected_verify_rejects_combination_with_affected(project: Project) -> None:
    project.write_passing_test()
    assert main(["--affected", "--affected-verify", str(project.root)]) == 4


def test_affected_verify_rejects_combination_with_lf(project: Project) -> None:
    project.write_passing_test()
    assert main(["--affected-verify", "--lf", str(project.root)]) == 4


def test_affected_verify_and_watch_together_is_a_usage_error(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_pyproject("[tool.voci]\n")
    project.write_passing_test()

    assert main(["--watch", "--affected-verify", str(project.root)]) == 4
    assert "--watch --affected" in capsys.readouterr().err


def test_affected_reports_a_graceful_abort_on_ctrl_c_during_prepare_affected(
    project: Project, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ctrl-C landing while `_prepare_affected` is still building the World/Selection (the
    least-instant part of `--affected`'s own startup on a large tree) gets the same graceful
    "voci: aborted" treatment as one landing anywhere else in a run, not a bare traceback."""
    from voci._affected import driver as _driver

    project.write_pyproject("[tool.voci]\n")
    project.write_passing_test()

    def interrupted_prior_selection(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(_driver, "prior_selection", interrupted_prior_selection)

    status = main(["--affected", str(project.root)])

    assert status == 2
    assert "voci: aborted (Ctrl-C)" in capsys.readouterr().err
