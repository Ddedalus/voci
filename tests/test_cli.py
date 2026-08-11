"""Tests for `velox.cli`: argument parsing, usage errors, config merging, and `main`
end to end (discover -> collect -> run -> report -> exit code).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _support import Project

import pytest
from velox import __version__
from velox.cli import _default_test_roots, build_parser, main


def _lines_starting_with(out: str, *prefixes: str, exclude_summary: bool = True) -> list[str]:
    """Lines of `out` starting with any of `prefixes`. Drops the short-summary line (which
    reuses the same prefixes but continues " - <reason>") unless `exclude_summary=False`.
    """
    lines = [line for line in out.splitlines() if line.startswith(prefixes)]
    return [line for line in lines if " - " not in line] if exclude_summary else lines


def test_version_is_a_string() -> None:
    assert isinstance(__version__, str)
    assert __version__


def test_build_parser_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_main_with_one_broken_module_and_nothing_else_exits_one(project: Project) -> None:
    """`main` returns its status rather than raising `SystemExit` (that mechanism belongs to
    the `if __name__ == "__main__": sys.exit(main())` block)."""
    project.write("test_broken.py", "raise RuntimeError('boom')\n")
    assert main([str(project.root)]) == 1


def test_main_all_passing_exits_zero(project: Project) -> None:
    """The exit code every CI green build actually depends on — exercised through the real
    discover -> collect -> run wiring, not just `_run.exit_code_for` in isolation."""
    project.write_passing_test()
    assert main([str(project.root)]) == 0


def test_main_empty_directory_exits_five(tmp_path: Path) -> None:
    assert main([str(tmp_path)]) == 5


def test_main_rejects_a_nonexistent_path_as_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd path and a genuinely empty suite must not look the same: a nonexistent path
    is a usage error (exit 4), not silently treated as zero tests collected (exit 5)."""
    missing = tmp_path / "does_not_exist"
    status = main([str(missing)])
    assert status == 4
    assert str(missing) in capsys.readouterr().err


def test_main_rejects_a_test_id_argument_as_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`path.py::test_name` is a plausible invocation, but test ids are not parsed -- that
    must be reported as a usage error, not fail as though the path were simply missing."""
    status = main(["tests/test_run.py::test_x"])
    assert status == 4
    assert "test ids" in capsys.readouterr().err


def test_default_roots_prefer_tests_dir_over_cwd(chdir_project: Project) -> None:
    (chdir_project.root / "tests").mkdir()
    assert _default_test_roots() == [Path("tests")]


def test_default_roots_fall_back_to_cwd_without_a_tests_dir(chdir_project: Project) -> None:
    assert _default_test_roots() == [Path()]


def test_rewrite_cache_with_plain_mode_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`plan` never resolves `--rewrite-cache` in `plain` mode, so passing both must be a
    usage error the user can act on, not a silently-ignored contradiction."""
    status = main(["--assert=plain", "--rewrite-cache=/tmp/wherever"])
    assert status == 4
    assert "--rewrite-cache" in capsys.readouterr().err


def test_main_runs_a_passing_and_a_failing_async_test(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: discover -> import -> run -> print -> exit code."""
    project.write(
        "test_sample.py",
        "async def test_pass():\n"
        "    assert 1 + 1 == 2\n"
        "\n"
        "async def test_fail():\n"
        "    x = 2\n"
        "    y = 3\n"
        "    assert x == y\n",
    )

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 1  # one failure present
    # `_report.Reporter` prints a per-file block, not a per-test line; both tests live in one
    # file, so the block is marked FAIL with a "(1 failed)" count. Matched as a whole line, not
    # an unanchored substring: `"FAIL" in out` would also match the unrelated "FAILED <id>"
    # failure-details header a few lines below.
    (block_line,) = _lines_starting_with(out, "PASS ", "FAIL ")
    assert block_line.startswith("FAIL")
    assert "test_sample.py" in block_line
    assert "2 tests" in block_line
    assert "(1 failed)" in block_line
    assert "::test_fail" in out
    # A bare, un-rewritten `assert` also raises `AssertionError` — asserting only that string
    # would pass even if the rewrite hook were never actually consulted. Assert on the
    # introspection text instead — `"assert 2 == 3"` is only ever produced by the AST rewrite.
    assert "assert 2 == 3" in out


def test_main_reports_a_setup_failure_as_error_not_failed(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fixture that raises during setup produces `Outcome.ERROR`, distinct from `FAILED` —
    end to end through `main`, covering both the per-test line (`ERROR`, not `FAILED`) and the
    summary's `errored` bucket."""
    project.write(
        "test_sample.py",
        "import velox\n\n"
        "@velox.fixture()\n"
        "def broken():\n"
        "    raise RuntimeError('setup boom')\n\n"
        "async def test_needs_it(value: int = velox.Depends(broken)):\n"
        "    pass\n",
    )

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 1
    # The failure-details section prints "ERROR <id>", not "<id> ERROR" -- pin that shape as a
    # whole line, not just that both substrings appear somewhere in the output. Excludes the
    # short-summary line further down, which also starts with "ERROR " but continues
    # `" - <reason>"`.
    (detail_line,) = _lines_starting_with(out, "ERROR ")
    assert "::test_needs_it" in detail_line
    assert "setup boom" in out
    assert "1 errored" in out


def test_bad_concurrency_value_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """`--concurrency` must be a positive integer -- `0`/negative is a usage error (exit 4)
    whose message interpolates the actual value given."""
    for bad in ("--concurrency=0", "--concurrency=-4"):
        status = main([bad])
        assert status == 4
        assert "--concurrency" in capsys.readouterr().err


def test_main_runs_end_to_end_with_a_custom_concurrency(project: Project) -> None:
    project.write_passing_test("test_sample.py")
    assert main([str(project.root), "--concurrency=2"]) == 0


def test_bad_timeout_value_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """`--timeout` must be a positive, finite number -- `0`, negative, `nan`, and `inf` are
    all usage errors (exit 4)."""
    for bad in ("--timeout=0", "--timeout=-1", "--timeout=nan", "--timeout=inf"):
        status = main([bad])
        assert status == 4
        assert "--timeout" in capsys.readouterr().err


def test_main_reports_a_timeout_as_its_own_outcome(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end `--timeout`: a hanging test surfaces `TIMEOUT`, distinct from `FAILED`/`ERROR`,
    in both the per-test line and the summary's `timed out` bucket."""
    project.write(
        "test_sample.py",
        "import asyncio\n\nasync def test_hangs():\n    await asyncio.sleep(10)\n",
    )

    status = main([str(project.root), "--timeout=0.05"])

    out = capsys.readouterr().out
    assert status == 1
    # Pin the failure-details header as a whole line ("TIMEOUT <id>").
    (detail_line,) = _lines_starting_with(out, "TIMEOUT ")
    assert "::test_hangs" in detail_line
    assert "1 timed out" in out


def test_main_reports_a_skipped_test_and_still_exits_zero(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `@velox.skip`-marked test must neither run for real nor fail the build over being
    skipped -- `skipped` contributes `0` to the exit code, same as `passed`."""
    project.write(
        "test_sample.py",
        "import velox\n\n"
        "@velox.skip('not ready')\n"
        "async def test_skipped():\n"
        "    raise AssertionError('must not run')\n",
    )

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 0
    assert "test_skipped SKIPPED (not ready)" in out


def test_main_prints_no_config_when_none_is_found(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_passing_test()

    assert main([str(project.root)]) == 0
    assert "config: none" in capsys.readouterr().out


def test_main_applies_tool_velox_env_before_the_first_test_import(
    chdir_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`env` from config is applied before the first test module import -- a test module
    reading `os.environ` at import time (not just inside a test body) must already see it."""
    monkeypatch.delenv("VELOX_CONFIG_SMOKE", raising=False)
    chdir_project.write_pyproject("[tool.velox]\nenv = { VELOX_CONFIG_SMOKE = 'from-config' }\n")
    chdir_project.write(
        "tests/test_env.py",
        "import os\n"
        "assert os.environ['VELOX_CONFIG_SMOKE'] == 'from-config'  # import time\n\n"
        "async def test_sees_it():\n"
        "    assert os.environ['VELOX_CONFIG_SMOKE'] == 'from-config'\n",
    )

    assert main([]) == 0


def test_main_restores_tool_velox_env_after_the_run(
    chdir_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[tool.velox] env` must not leak from one `main` call into the next -- covers both a key
    that already existed (restored to its old value) and one that didn't (removed again)."""
    monkeypatch.setenv("VELOX_CONFIG_PREEXISTING", "original")
    monkeypatch.delenv("VELOX_CONFIG_NEW", raising=False)
    chdir_project.write_pyproject(
        "[tool.velox]\n"
        "env = { VELOX_CONFIG_PREEXISTING = 'overridden', VELOX_CONFIG_NEW = 'added' }\n"
    )
    chdir_project.write_passing_test()

    assert main([]) == 0

    assert os.environ["VELOX_CONFIG_PREEXISTING"] == "original"
    assert "VELOX_CONFIG_NEW" not in os.environ


def test_main_config_testpaths_is_used_when_no_paths_are_given(chdir_project: Project) -> None:
    chdir_project.write_pyproject("[tool.velox]\ntestpaths = ['suite']\n")
    chdir_project.write("suite/test_it.py", "async def test_it():\n    pass\n")
    # A `tests/` dir also exists, empty -- proves `testpaths` wins over the built-in default,
    # not just that `suite/` happens to be found some other way.
    (chdir_project.root / "tests").mkdir()

    status = main([])

    assert status == 0


def test_main_empty_config_testpaths_means_no_tests_not_the_built_in_default(
    chdir_project: Project,
) -> None:
    """`testpaths = []` must not be treated the same as "unset" and silently fall back to
    `_default_test_roots`."""
    chdir_project.write_pyproject("[tool.velox]\ntestpaths = []\n")
    chdir_project.write("tests/test_it.py", "async def test_it():\n    pass\n")

    assert main([]) == 5  # no tests collected, not "1 passed"


def test_main_cli_concurrency_overrides_config(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """`[tool.velox] concurrency = 0` would be a usage error if it were ever consulted -- passing
    `--concurrency=2` on the command line must win instead of the merge falling through to the
    bad config value: CLI flags take priority over `[tool.velox]`."""
    chdir_project.write_pyproject("[tool.velox]\nconcurrency = 0\n")
    chdir_project.write_passing_test()

    assert main(["--concurrency=2"]) == 0


def test_main_rejects_a_bad_config_concurrency_value_as_a_usage_error(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Named by its actual source (the `pyproject.toml` `[tool.velox]` set it in), not
    `--concurrency` -- the user never touched that flag, and a message pointing at it would send
    them looking in the wrong place."""
    chdir_project.write_pyproject("[tool.velox]\nconcurrency = 0\n")
    chdir_project.write_passing_test()

    status = main([])

    err = capsys.readouterr().err
    assert status == 4
    assert "--concurrency" not in err
    assert str(chdir_project.root / "pyproject.toml") in err
    assert "concurrency" in err


def test_main_rejects_a_bad_config_timeout_value_as_a_usage_error(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    chdir_project.write_pyproject("[tool.velox]\ntimeout = -1\n")
    chdir_project.write_passing_test()

    status = main([])

    err = capsys.readouterr().err
    assert status == 4
    assert "--timeout" not in err
    assert str(chdir_project.root / "pyproject.toml") in err


def test_main_config_test_file_patterns_empty_list_means_no_files_not_the_built_in_default(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """`test_file_patterns = []` must not be treated the same as "unset" and silently fall
    back to the built-in `test_*.py` pattern."""
    chdir_project.write_pyproject("[tool.velox]\ntest_file_patterns = []\n")
    chdir_project.write("test_ok.py", "async def test_ok():\n    raise AssertionError\n")

    assert main([]) == 5  # no tests collected, not "1 failed"


def test_main_nonexistent_config_testpath_entry_is_a_usage_error(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd `[tool.velox] testpaths` entry is a usage error (exit 4), not a silent
    "0 tests"."""
    chdir_project.write_pyproject("[tool.velox]\ntestpaths = ['tset']\n")
    chdir_project.write("tests/test_it.py", "async def test_it():\n    pass\n")

    status = main([])

    err = capsys.readouterr().err
    assert status == 4
    assert "tset" in err


def test_main_env_is_restored_even_when_rewrite_install_fails_after_it_is_applied(
    chdir_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `env` mutation happens inside the same `try` the restore guards, so even an
    exception from `_rewrite.install` itself must still be undone -- not leak `[tool.velox]
    env` into the process."""
    monkeypatch.delenv("VELOX_CONFIG_INSTALL_FAILS", raising=False)
    chdir_project.write_pyproject(
        "[tool.velox]\nenv = { VELOX_CONFIG_INSTALL_FAILS = 'leaked' }\n"
    )
    chdir_project.write_passing_test()
    monkeypatch.setattr(
        "velox._assertions.rewrite.install",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated install failure")),
    )

    with pytest.raises(RuntimeError, match="simulated install failure"):
        main([])

    assert "VELOX_CONFIG_INSTALL_FAILS" not in os.environ


def test_main_rejects_an_invalid_tool_velox_table_as_a_usage_error(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    chdir_project.write_pyproject("[tool.velox]\nnot_a_real_key = 1\n")

    status = main([])

    assert status == 4
    assert "not_a_real_key" in capsys.readouterr().err


def test_main_config_test_file_patterns_and_ignore_are_honored(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    chdir_project.write_pyproject(
        "[tool.velox]\ntest_file_patterns = ['check_*.py']\nignore = ['skip_me']\n"
    )
    chdir_project.write("check_one.py", "async def test_one():\n    pass\n")
    # Would normally match the built-in `test_*.py` pattern -- must be ignored now that
    # `test_file_patterns` no longer includes it.
    chdir_project.write("test_two.py", "async def test_two():\n    raise AssertionError\n")
    chdir_project.write(
        "skip_me/check_three.py", "async def test_three():\n    raise AssertionError\n"
    )

    status = main([])

    out = capsys.readouterr().out
    assert status == 0
    assert "check_one.py" in out
    assert "test_two.py" not in out
    assert "check_three.py" not in out


def test_main_prints_jest_style_per_file_blocks_end_to_end(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through `main`: two files, one all-passing and one with a failure, produce two
    per-file scrollback blocks plus a failure-details/short-summary section and the wall-vs-Σ
    final line.

    Every assertion below matches a whole *line*, not an unanchored substring somewhere in
    `out`.
    """
    project.write(
        "test_a.py", "async def test_one():\n    pass\n\nasync def test_two():\n    pass\n"
    )
    project.write(
        "test_b.py",
        "async def test_broken():\n    total = 3\n    assert total == 4, 'widget count'\n",
    )

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 1

    lines = out.splitlines()
    non_empty_lines = [line for line in lines if line.strip()]
    block_lines = _lines_starting_with(out, "PASS ", "FAIL ")
    assert len(block_lines) == 2  # one block per file, not per test
    (pass_line,) = [line for line in block_lines if line.startswith("PASS")]
    (fail_line,) = [line for line in block_lines if line.startswith("FAIL")]
    assert "test_a.py" in pass_line
    assert "2 tests" in pass_line
    assert "test_b.py" in fail_line
    assert "1 tests" in fail_line
    assert "(1 failed)" in fail_line

    # Failure details + short test summary, in logical order. The failure-details header ("FAILED
    # <id>") and the short-summary line further down ("FAILED <id> - <reason>") both start with
    # "FAILED ", so exclude the latter by the " - " it always has and the former never does.
    assert "--- short test summary ---" in out
    (detail_line,) = _lines_starting_with(out, "FAILED ")
    assert "::test_broken" in detail_line
    # The user's own message, not the rewriter's `assert 3 == 4` explanation, which the vendored
    # rewriter appends *after* the exception line under the default `--assert=rewrite`.
    (summary_line,) = [line for line in lines if "test_broken -" in line]
    assert summary_line.endswith("AssertionError: widget count")

    # Wall-vs-Σ is the run's proof-of-value metric, and must be genuinely the *last* line --
    # after the skip list, collection-error tracebacks, and the `N tests: ...` summary.
    assert "tests ·" in non_empty_lines[-1]
    assert "wall (Σ" in non_empty_lines[-1]


def test_main_prepends_rootdir_to_sys_path_for_absolute_imports(chdir_project: Project) -> None:
    """`rootdir` goes on `sys.path` once, before the first test module import, so a plain
    absolute import rooted at `rootdir` -- a sibling package, or a shared `tests/fixtures.py`
    -- resolves without an `__init__.py` anywhere (PEP 420 namespace packages)."""
    chdir_project.write("relay/__init__.py", "VALUE = 42\n")
    chdir_project.write("tests/fixtures.py", "SHARED = 'shared-value'\n")
    chdir_project.write(
        "tests/test_imports.py",
        "from relay import VALUE\n"
        "from tests.fixtures import SHARED\n\n"
        "async def test_sees_both():\n"
        "    assert VALUE == 42\n"
        "    assert SHARED == 'shared-value'\n",
    )

    assert main([]) == 0


def test_main_removes_rootdir_from_sys_path_after_the_run(chdir_project: Project) -> None:
    """The `rootdir` insertion must not leak from one `main` call into the next, or grow
    `sys.path` without bound over many calls."""
    chdir_project.write_passing_test()
    rootdir_str = str(chdir_project.root.resolve())
    assert rootdir_str not in sys.path

    assert main([]) == 0
    assert rootdir_str not in sys.path

    assert main([]) == 0
    assert rootdir_str not in sys.path


def test_main_does_not_disturb_a_preexisting_sys_path_entry_for_rootdir(
    chdir_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `rootdir` was already on `sys.path` before this call (an embedder's own setup, or a
    nested `main()`), this call must not remove it on the way out."""
    chdir_project.write_passing_test()
    rootdir_str = str(chdir_project.root.resolve())
    monkeypatch.syspath_prepend(rootdir_str)
    assert rootdir_str in sys.path

    assert main([]) == 0
    assert rootdir_str in sys.path
