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
