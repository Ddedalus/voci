"""Tests for `velox.cli`: argument parsing, usage errors, config merging, and `main`
end to end (discover -> collect -> run -> report -> exit code).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest
from _support import Project

from velox import __version__
from velox.cli import _default_test_roots, _friendly_path, build_parser, main


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


def test_main_runs_a_suite_wired_with_annotated_injections(project: Project) -> None:
    """The annotated spelling, end to end and in the hardest module velox has to read one in:
    annotations stringified by `from __future__ import annotations`, a type that exists only
    under `TYPE_CHECKING`, an alias, a fixture declaring its own dependency the same way, and
    `@velox.parametrize` supplying a parameter that now *follows* an injected one with no
    default — the signature shape the default-position form could never produce.
    """
    project.write(
        "test_sample.py",
        "from __future__ import annotations\n\n"
        "from typing import TYPE_CHECKING, Annotated\n\n"
        "import velox\n"
        "from velox import Depends\n\n"
        "if TYPE_CHECKING:\n"
        "    from decimal import Decimal\n\n"
        "@velox.fixture()\n"
        "def base() -> int:\n"
        "    return 2\n\n"
        "@velox.fixture()\n"
        "def doubled(value: Annotated[int, Depends(base)]) -> int:\n"
        "    return value * 2\n\n"
        "type Doubled = Annotated[int, Depends(doubled)]\n\n"
        "async def test_annotated(value: Doubled) -> None:\n"
        "    assert value == 4\n\n"
        "async def test_unresolvable_type(value: Annotated[Decimal, Depends(doubled)]) -> None:\n"
        "    assert value == 4\n\n"
        "@velox.parametrize('expected', [4])\n"
        "async def test_before_a_parametrized_argument(\n"
        "    value: Annotated[int, Depends(doubled)], expected: int\n"
        ") -> None:\n"
        "    assert value == expected\n",
    )

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


def test_main_rejects_a_test_id_whose_file_is_missing_as_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The file part of an id is checked exactly like a bare path: a typo there must not walk
    to nothing and exit 5 as though the suite were empty."""
    status = main(["tests/does_not_exist.py::test_x"])
    assert status == 4
    assert "does not exist" in capsys.readouterr().err


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


@pytest.mark.parametrize("bad", ["--concurrency=0", "--concurrency=-4"])
def test_bad_concurrency_value_is_a_usage_error(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--concurrency` must be a positive integer -- `0`/negative is a usage error (exit 4)
    whose message interpolates the actual value given."""
    status = main([bad])
    assert status == 4
    assert "--concurrency" in capsys.readouterr().err


def test_main_runs_end_to_end_with_a_custom_concurrency(project: Project) -> None:
    project.write_passing_test("test_sample.py")
    assert main([str(project.root), "--concurrency=2"]) == 0


@pytest.mark.parametrize("bad", ["--timeout=0", "--timeout=-1", "--timeout=nan", "--timeout=inf"])
def test_bad_timeout_value_is_a_usage_error(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    """`--timeout` must be a positive, finite number -- `0`, negative, `nan`, and `inf` are
    all usage errors (exit 4)."""
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
    assert "1 test · 1 skipped" in out


def test_main_reports_a_skipped_tests_reason_under_dash_v(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_sample.py",
        "import velox\n\n"
        "@velox.skip('not ready')\n"
        "async def test_skipped():\n"
        "    raise AssertionError('must not run')\n",
    )

    status = main([str(project.root), "-v"])

    out = capsys.readouterr().out
    assert status == 0
    assert "test_sample.py::test_skipped - not ready" in out


def test_main_leading_test_count_includes_skipped(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The leading `N tests` count is everything collection found, not just what ran --
    `len(results)` alone would undercount by the skipped tests it never sees."""
    project.write(
        "test_sample.py",
        "import velox\n\n"
        "@velox.skip('not ready')\n"
        "async def test_skipped():\n"
        "    raise AssertionError('must not run')\n\n"
        "async def test_runs():\n"
        "    pass\n",
    )

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 0
    # 1 ran + 1 skipped = 2, not the 1 that len(results) alone would say. The skip is counted
    # against its own file's block too, rather than spending a line of its own on the reason.
    assert "2 tests · 1 passed · 1 skipped" in out
    (block_line,) = _lines_starting_with(out, "PASS ", "FAIL ")
    assert "(1 skipped)" in block_line
    assert "not ready" not in out


def test_main_colors_output_on_a_tty_and_stays_plain_off_one(
    project: Project, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`capsys`'s captured stdout isn't a tty, so the default run (below) is already the
    plain-output proof; this pins the affirmative case, and that `NO_COLOR` overrides a
    tty right back to plain."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    project.write_passing_test()

    assert main([str(project.root)]) == 0
    assert "\x1b[" not in capsys.readouterr().out

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert main([str(project.root)]) == 0
    assert "\x1b[" in capsys.readouterr().out

    monkeypatch.setenv("NO_COLOR", "1")
    assert main([str(project.root)]) == 0
    assert "\x1b[" not in capsys.readouterr().out


def test_main_dash_m_runs_only_matching_tags_and_reports_the_rest_deselected(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_sample.py",
        "import velox\n\n"
        "@velox.tag('slow')\n"
        "async def test_slow():\n"
        "    pass\n\n"
        "async def test_fast():\n"
        "    pass\n",
    )

    status = main([str(project.root), "-m", "slow"])

    out = capsys.readouterr().out
    assert status == 0
    (block_line,) = _lines_starting_with(out, "PASS ", "FAIL ")
    assert "test_sample.py" in block_line
    assert "1 test " in block_line
    assert "1 test · 1 passed · 1 deselected" in out


def test_main_dash_m_matching_nothing_exits_five(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every test deselected reads the same as a genuinely empty suite: exit `5`."""
    project.write_passing_test()

    status = main([str(project.root), "-m", "slow"])

    out = capsys.readouterr().out
    assert status == 5
    assert "1 deselected" in out


def test_main_omits_deselected_when_dash_m_matches_everything(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A category with nothing to report is left out of the summary entirely, `-m` in effect
    or not: `0 deselected` is one more number to read past on a line that had nothing to say."""
    project.write_passing_test()

    status = main([str(project.root), "-m", "not slow"])

    out = capsys.readouterr().out
    assert status == 0
    assert "deselected" not in out


def test_main_dash_m_never_reclassifies_a_skip_marked_test_as_deselected(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A test that is both skip-marked and tag-excluded by `-m` must stay counted as skipped --
    and the exit code must not depend on whether `-m` happened to also exclude it."""
    project.write(
        "test_sample.py",
        "import velox\n\n"
        "@velox.tag('slow')\n"
        "@velox.skip('not ready')\n"
        "async def test_skipped():\n"
        "    raise AssertionError('must not run')\n",
    )

    without_m = main([str(project.root)])
    out_without_m = capsys.readouterr().out
    with_m = main([str(project.root), "-m", "not slow"])
    out_with_m = capsys.readouterr().out

    assert without_m == with_m == 0
    assert "1 test · 1 skipped" in out_without_m
    assert "1 test · 1 skipped" in out_with_m
    assert "deselected" not in out_without_m


def test_main_without_dash_m_never_deselects(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_sample.py",
        "import velox\n\n@velox.tag('slow')\nasync def test_slow():\n    pass\n",
    )

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 0
    assert "deselected" not in out


def test_main_rejects_an_invalid_dash_m_expression_as_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(tmp_path), "-m", "slow =="]) == 4
    assert "invalid -m expression" in capsys.readouterr().err


def test_main_says_nothing_about_assertions_in_the_default_happy_path(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """`rewrite` succeeding is what every run does unless told otherwise -- announcing it
    would be reporting the default as news."""
    project.write_passing_test()

    assert main([str(project.root)]) == 0
    assert "assertions:" not in capsys.readouterr().out


def test_main_announces_assert_plain_when_chosen_on_purpose(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_passing_test()

    assert main([str(project.root), "--assert=plain"]) == 0
    assert "assertions: plain" in capsys.readouterr().out


def test_main_prints_no_config_when_none_is_found(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_passing_test()

    assert main([str(project.root)]) == 0
    assert "config: none" in capsys.readouterr().out


def test_main_prints_the_config_path_relative_to_cwd(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The absolute form is noise when the file is right there under cwd."""
    chdir_project.write_pyproject("[tool.velox]\n")
    chdir_project.write_passing_test()

    assert main([]) == 0
    assert "config: pyproject.toml" in capsys.readouterr().out


def test_friendly_path_stays_relative_a_couple_of_hops_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    nested = tmp_path / "examples" / "01-fastapi-crud" / "pyproject.toml"
    assert _friendly_path(nested) == str(Path("examples") / "01-fastapi-crud" / "pyproject.toml")


def test_friendly_path_stays_relative_within_the_up_hop_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "a" / "b")
    assert _friendly_path(tmp_path / "config.toml") == str(Path("..") / ".." / "config.toml")


def test_friendly_path_goes_absolute_past_the_up_hop_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    far = tmp_path / "elsewhere" / "config.toml"
    assert _friendly_path(far) == str(far)


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
    chdir_project.write_pyproject("[tool.velox]\nenv = { VELOX_CONFIG_INSTALL_FAILS = 'leaked' }\n")
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
    assert "1 test " in fail_line
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

    # The run ends on its totals, after every detail section, with what went wrong on the one
    # line above them.
    assert non_empty_lines[-2] == "1 failed"
    assert non_empty_lines[-1].startswith("3 tests · 2 passed · ")
    assert "wall (" in non_empty_lines[-1]
    assert "concurrency)" in non_empty_lines[-1]


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


# @velox.isolated: the per-test subprocess tier, end to end (each of these actually spawns a
# subprocess -- slower than the rest of this file by construction, not a bug).
# -----------------------------------------------------------------------------------------


def test_main_runs_a_passing_isolated_test(project: Project) -> None:
    project.write(
        "test_iso.py",
        "import velox\n\n@velox.isolated\nasync def test_ok():\n    assert 1 + 1 == 2\n",
    )
    assert main([str(project.root)]) == 0


def test_isolated_test_runs_in_a_different_process(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the mark: the test body observes a different `os.getpid()` than the
    process running `main()`. `PARENT_PID` is read from an env var, not computed at module import
    time -- the subprocess re-imports this file fresh, so a module-level `os.getpid()` would just
    read back the subprocess's own pid and pass either way.
    """
    monkeypatch.setenv("ISO_TEST_PARENT_PID", str(os.getpid()))
    project.write(
        "test_iso.py",
        "import os\nimport velox\n\n"
        "@velox.isolated\n"
        "async def test_elsewhere():\n"
        "    assert os.getpid() != int(os.environ['ISO_TEST_PARENT_PID'])\n",
    )
    assert main([str(project.root)]) == 0


def test_main_reports_a_failing_isolated_test_with_assertion_introspection(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_iso.py",
        "import velox\n\n"
        "@velox.isolated\n"
        "async def test_bad():\n"
        "    x = 2\n"
        "    y = 3\n"
        "    assert x == y\n",
    )

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 1
    assert "::test_bad" in out
    # Same proof as the in-process case: only the AST rewrite ever produces this exact text.
    assert "assert 2 == 3" in out


def test_main_shows_captured_output_for_a_failing_isolated_test(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_iso.py",
        "import velox\n\n"
        "@velox.isolated\n"
        "async def test_bad():\n"
        "    print('from the subprocess')\n"
        "    assert False\n",
    )

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 1
    assert "from the subprocess" in out


def test_isolated_test_gets_a_working_tmp_path(project: Project) -> None:
    project.write(
        "test_iso.py",
        "from pathlib import Path\n"
        "import velox\n\n"
        "@velox.isolated\n"
        "async def test_writes_a_file(tmp_path: Path = velox.Depends(velox.tmp_path)):\n"
        "    (tmp_path / 'f.txt').write_text('hi')\n"
        "    assert (tmp_path / 'f.txt').read_text() == 'hi'\n",
    )
    assert main([str(project.root)]) == 0


def test_main_mixes_isolated_and_in_process_tests_in_one_run(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_mixed.py",
        "import velox\n\n"
        "async def test_in_process_pass():\n    pass\n\n"
        "async def test_in_process_fail():\n    assert False\n\n"
        "@velox.isolated\n"
        "async def test_isolated_pass():\n    pass\n\n"
        "@velox.isolated\n"
        "async def test_isolated_fail():\n    assert False\n",
    )

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 1
    assert "2 failed" in out
    assert "4 tests · 2 passed" in out
    assert "::test_in_process_fail" in out
    assert "::test_isolated_fail" in out


def _write_selection_suite(project: Project) -> None:
    """Two files, five tests -- a class group and a parametrized test among them, so one suite
    serves every selection, ordering and early-stop assertion below."""
    project.write(
        "test_users.py",
        "import velox\n\n"
        "async def test_create():\n    pass\n\n"
        "@velox.parametrize('role', ['admin', 'guest'])\n"
        "async def test_role(role):\n    pass\n\n"
        "class TestDelete:\n"
        "    async def test_soft(self):\n        pass\n",
    )
    project.write("test_orders.py", "async def test_place():\n    pass\n")


def test_main_runs_one_test_by_id(project: Project, capsys: pytest.CaptureFixture[str]) -> None:
    _write_selection_suite(project)

    status = main([f"{project.root / 'test_users.py'}::test_create"])

    out = capsys.readouterr().out
    assert status == 0
    assert "1 test · 1 passed" in out
    assert "3 deselected" in out


def test_main_runs_one_class_grouped_test_by_id(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    status = main([f"{project.root / 'test_users.py'}::TestDelete::test_soft", "--collect-only"])

    out = capsys.readouterr().out
    assert status == 0
    assert "test_users.py::TestDelete::test_soft" in out
    assert "1 test collected" in out


def test_main_id_naming_a_class_selects_every_test_in_it(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_group.py",
        "class TestGroup:\n"
        "    async def test_a(self):\n        pass\n\n"
        "    async def test_b(self):\n        pass\n\n"
        "async def test_outside():\n    pass\n",
    )

    status = main([f"{project.root / 'test_group.py'}::TestGroup", "--collect-only"])

    out = capsys.readouterr().out
    assert status == 0
    assert "2 tests collected" in out
    assert "::test_outside" not in out


def test_main_id_selects_one_parametrize_case(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    status = main([f"{project.root / 'test_users.py'}::test_role[admin]", "--collect-only"])

    out = capsys.readouterr().out
    assert status == 0
    assert "test_users.py::test_role[admin]" in out
    assert "[guest]" not in out


def test_main_id_naming_a_function_selects_all_of_its_cases(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    status = main([f"{project.root / 'test_users.py'}::test_role", "--collect-only"])

    out = capsys.readouterr().out
    assert status == 0
    assert "2 tests collected" in out


def test_main_rejects_an_id_that_matches_no_test_as_a_usage_error(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same reasoning as a path that doesn't exist, one level down: a mistyped test name would
    otherwise select nothing and exit 5, indistinguishable from an empty file."""
    _write_selection_suite(project)

    status = main([f"{project.root / 'test_users.py'}::test_typo"])

    assert status == 4
    assert "test_typo" in capsys.readouterr().err


def test_main_an_id_in_a_file_that_fails_to_import_reports_the_import_error(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ids that file would have contributed are unknowable, so "no test matches" would be a
    guess -- and it would bury the traceback that actually explains the run."""
    project.write("test_broken.py", "raise RuntimeError('boom')\n")

    status = main([f"{project.root / 'test_broken.py'}::test_anything"])

    captured = capsys.readouterr()
    assert status == 1
    assert "COLLECTION ERROR" in captured.out
    assert "boom" in captured.out
    assert "no test matches" not in captured.err


def test_main_an_id_leaves_a_skipped_test_it_does_not_name_out_of_the_run(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asking for one test is asking about that test: a skip elsewhere in the file is not part
    of the answer, and is not counted in the run either."""
    project.write(
        "test_sample.py",
        "import velox\n\n"
        "async def test_wanted():\n    pass\n\n"
        "@velox.skip('later')\n"
        "async def test_other():\n    pass\n",
    )

    status = main([f"{project.root / 'test_sample.py'}::test_wanted"])

    out = capsys.readouterr().out
    assert status == 0
    assert "1 test · 1 passed" in out
    assert "skipped" not in out


def test_main_k_leaves_a_skipped_test_it_does_not_match_out_of_the_run(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_sample.py",
        "import velox\n\n"
        "async def test_wanted():\n    pass\n\n"
        "@velox.skip('later')\n"
        "async def test_other():\n    pass\n",
    )

    status = main([str(project.root), "-k", "wanted"])

    out = capsys.readouterr().out
    assert status == 0
    assert "1 test · 1 passed" in out
    assert "skipped" not in out


def test_main_an_id_naming_a_case_of_a_skipped_test_reports_the_skip(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A skipped test is reported under its bare name, its cases never having been built, so a
    case selector has to reach it -- the alternative is calling a real test id a typo."""
    project.write(
        "test_sample.py",
        "import velox\n\n"
        "@velox.skip('later')\n"
        "@velox.parametrize('role', ['admin', 'guest'])\n"
        "async def test_role(role):\n    pass\n",
    )

    status = main([f"{project.root / 'test_sample.py'}::test_role[admin]"])

    captured = capsys.readouterr()
    assert status == 0
    assert "1 test · 1 skipped" in captured.out
    assert "no test matches" not in captured.err


def test_main_k_matches_a_skipped_test_by_the_id_it_has(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `-k` term is matched against the id a skipped test actually carries, cases and all
    still unbuilt -- so a term naming the test finds it, and one naming a case does not."""
    project.write(
        "test_sample.py",
        "import velox\n\n"
        "@velox.skip('later')\n"
        "@velox.parametrize('role', ['admin', 'guest'])\n"
        "async def test_role(role):\n    pass\n",
    )

    assert main([str(project.root), "-k", "role"]) == 0
    assert "1 test · 1 skipped" in capsys.readouterr().out
    assert main([str(project.root), "-k", "admin"]) == 5
    assert "skipped" not in capsys.readouterr().out


def test_main_an_id_naming_a_case_of_a_test_dash_m_excluded_is_an_empty_run(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-m` excludes a test before its cases are built, so the case id it would have carried is
    never spelled out anywhere -- and naming it is an empty intersection, not a typo."""
    project.write(
        "test_sample.py",
        "import velox\n\n"
        "@velox.tag('slow')\n"
        "@velox.parametrize('role', ['admin', 'guest'])\n"
        "async def test_role(role):\n    pass\n",
    )

    status = main([f"{project.root / 'test_sample.py'}::test_role[admin]", "-m", "not slow"])

    assert status == 5
    assert "no test matches" not in capsys.readouterr().err


def test_main_rejects_a_case_id_on_a_test_that_has_no_cases(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of the rule above: a `[case]` on a test whose cases *are* known, and that
    has none by that name, is the typo it looks like."""
    _write_selection_suite(project)

    status = main([f"{project.root / 'test_users.py'}::test_create[admin]"])

    assert status == 4
    assert "test_create[admin]" in capsys.readouterr().err


def test_main_an_id_deselected_by_k_is_an_empty_run_not_a_usage_error(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two selection flags that intersect to nothing is a thing the user asked for; only an id
    matching no collected test at all is a typo."""
    _write_selection_suite(project)

    status = main([f"{project.root / 'test_users.py'}::test_create", "-k", "orders"])

    assert status == 5
    assert "no test matches" not in capsys.readouterr().err


def test_main_a_directory_argument_widens_an_id_for_a_file_under_it(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    status = main(
        [str(project.root), f"{project.root / 'test_users.py'}::test_create", "--collect-only"]
    )

    out = capsys.readouterr().out
    assert status == 0
    assert "5 tests collected" in out


def test_main_rejects_an_empty_id_as_a_usage_error(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    status = main([f"{project.root / 'test_users.py'}::"])

    assert status == 4
    assert "::" in capsys.readouterr().err


def test_main_rejects_an_id_on_a_directory_as_a_usage_error(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    status = main([f"{project.root}::test_create"])

    assert status == 4
    assert "directory" in capsys.readouterr().err


def test_main_k_selects_by_substring_of_the_id(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    status = main([str(project.root), "-k", "orders"])

    out = capsys.readouterr().out
    assert status == 0
    assert "1 test · 1 passed" in out
    assert "4 deselected" in out


def test_main_k_matches_a_parametrize_case_id(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-k` is applied after expansion, so a case id is matchable -- the reason it can't be a
    cheap per-function filter."""
    _write_selection_suite(project)

    status = main([str(project.root), "-k", "admin", "--collect-only"])

    out = capsys.readouterr().out
    assert status == 0
    assert "test_users.py::test_role[admin]" in out
    assert "1 test collected" in out


def test_main_k_supports_boolean_operators(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    status = main([str(project.root), "-k", "users and not role", "--collect-only"])

    out = capsys.readouterr().out
    assert status == 0
    assert "2 tests collected" in out


def test_main_rejects_a_malformed_k_expression_as_a_usage_error(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    status = main([str(project.root), "-k", "users and"])

    assert status == 4
    assert "-k" in capsys.readouterr().err


def test_main_collect_only_prints_ids_and_runs_nothing(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_sample.py",
        "async def test_one():\n    raise AssertionError('must not run')\n",
    )

    status = main([str(project.root), "--collect-only"])

    out = capsys.readouterr().out
    # Exit 0 with a test that fails when run: nothing was run.
    assert status == 0
    assert "test_sample.py::test_one" in out
    assert "1 test collected" in out
    assert "wall" not in out


def test_main_takes_a_collect_only_id_back_as_an_argument_from_a_subdirectory(
    chdir_project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The round trip `--collect-only` exists for: an id it printed, pasted back as an argument
    from wherever the reader happens to be standing."""
    chdir_project.write_pyproject("[tool.velox]\n")
    chdir_project.write("tests/test_sample.py", "async def test_one():\n    pass\n")
    assert main(["--collect-only"]) == 0
    printed = capsys.readouterr().out.splitlines()[-2]
    assert printed == "tests/test_sample.py::test_one"

    monkeypatch.chdir(chdir_project.root / "tests")
    status = main([printed])

    out = capsys.readouterr().out
    assert status == 0
    assert "1 test · 1 passed" in out


def test_main_leaves_a_missing_path_alone_without_a_tool_velox_table(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no config file the rootdir is derived from the arguments, so re-reading one against
    it would make an argument's meaning depend on what it was typed alongside."""
    chdir_project.write("sub/test_a.py", "async def test_a():\n    pass\n")
    chdir_project.write("sub/test_b.py", "async def test_b():\n    pass\n")

    status = main(["sub/test_a.py", "test_b.py"])

    assert status == 4
    assert "path does not exist: 'test_b.py'" in capsys.readouterr().err


def test_main_prefers_the_local_reading_of_a_path_that_exists(
    chdir_project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rootdir reading is the fallback, not the rule: an argument that names something from
    here means that, whatever the same spelling would name under the rootdir."""
    chdir_project.write_pyproject("[tool.velox]\n")
    chdir_project.write("tests/test_sample.py", "async def test_root_level():\n    pass\n")
    chdir_project.write("tests/tests/test_sample.py", "async def test_nested():\n    pass\n")

    monkeypatch.chdir(chdir_project.root / "tests")
    status = main(["tests/test_sample.py", "--collect-only"])

    out = capsys.readouterr().out
    assert status == 0
    assert "::test_nested" in out
    assert "::test_root_level" not in out


def test_main_collect_only_exits_five_when_nothing_is_collected(tmp_path: Path) -> None:
    assert main([str(tmp_path), "--collect-only"]) == 5


def test_main_collect_only_reports_a_collection_error_and_exits_one(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inspecting a suite is exactly when an unimportable file matters most."""
    project.write("test_broken.py", "raise RuntimeError('boom')\n")

    status = main([str(project.root), "--collect-only"])

    assert status == 1
    assert "COLLECTION ERROR" in capsys.readouterr().out


def test_main_x_stops_after_the_first_failure(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--serial` so the stop point is deterministic: tests start in collection order, so
    everything after the first failure is left unstarted."""
    project.write(
        "test_sample.py",
        "async def test_one():\n    assert False\n\n"
        "async def test_two():\n    assert False\n\n"
        "async def test_three():\n    assert False\n",
    )

    status = main([str(project.root), "-x", "--serial"])

    out = capsys.readouterr().out
    assert status == 1
    assert "1 failed" in out
    assert "3 tests · " in out
    assert "2 not run (--maxfail)" in out
    assert "stopped after 1 failed" in out


def test_main_maxfail_counts_up_to_its_threshold(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_sample.py",
        "async def test_one():\n    assert False\n\n"
        "async def test_two():\n    assert False\n\n"
        "async def test_three():\n    assert False\n",
    )

    status = main([str(project.root), "--maxfail=2", "--serial"])

    out = capsys.readouterr().out
    assert status == 1
    assert "2 failed" in out
    assert "1 not run (--maxfail)" in out


def test_main_maxfail_does_not_stop_a_passing_run(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    status = main([str(project.root), "-x"])

    out = capsys.readouterr().out
    assert status == 0
    assert "5 tests · 5 passed" in out
    assert "not run" not in out


@pytest.mark.parametrize("bad", ["--maxfail=0", "--maxfail=-2"])
def test_bad_maxfail_value_is_a_usage_error(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    status = main([bad])
    assert status == 4
    assert "--maxfail" in capsys.readouterr().err


def test_x_contradicting_maxfail_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """-x *is* --maxfail=1; a run that silently ignored one of the two would report the wrong
    thing about what it did."""
    status = main(["-x", "--maxfail=3"])
    assert status == 4
    assert "-x" in capsys.readouterr().err


def test_main_serial_runs_exactly_one_test_at_a_time(project: Project) -> None:
    project.write(
        "test_sample.py",
        "import asyncio\n\n"
        "running = 0\n\n"
        "async def _one():\n"
        "    global running\n"
        "    running += 1\n"
        "    assert running == 1\n"
        "    await asyncio.sleep(0)\n"
        "    running -= 1\n\n"
        "async def test_a():\n    await _one()\n\n"
        "async def test_b():\n    await _one()\n",
    )

    assert main([str(project.root), "--serial"]) == 0


def test_serial_contradicting_concurrency_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = main(["--serial", "--concurrency=4"])
    assert status == 4
    assert "--serial" in capsys.readouterr().err


def test_main_verbose_prints_a_line_per_test(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    status = main([str(project.root), "-v"])

    out = capsys.readouterr().out
    assert status == 0
    assert len(_lines_starting_with(out, "PASSED ")) == 5
    assert "::test_create" in out


def test_main_quiet_prints_one_character_per_file_and_no_header(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    status = main([str(project.root), "-q"])

    out = capsys.readouterr().out
    assert status == 0
    assert "config:" not in out
    assert _lines_starting_with(out, "PASS ") == []
    # One character per file, on a line of their own.
    assert out.splitlines()[0] == ".."


def test_main_quiet_still_prints_failure_detail(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """What a run *found* never gets quieter -- only how it narrates its progress does."""
    project.write("test_sample.py", "async def test_bad():\n    x = 1\n    assert x == 2\n")

    status = main([str(project.root), "-q"])

    out = capsys.readouterr().out
    assert status == 1
    assert "assert 1 == 2" in out
    assert "--- short test summary ---" in out


def test_main_durations_lists_the_slowest_tests(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_sample.py",
        "import asyncio\n\n"
        "async def test_slow():\n    await asyncio.sleep(0.05)\n\n"
        "async def test_fast():\n    pass\n",
    )

    status = main([str(project.root), "--durations=1"])

    out = capsys.readouterr().out
    assert status == 0
    assert "--- slowest 1 test ---" in out
    duration_line = out.split("--- slowest 1 test ---\n")[1].splitlines()[0]
    assert "::test_slow" in duration_line


def test_main_prints_no_durations_section_by_default(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_selection_suite(project)

    main([str(project.root)])

    assert "slowest" not in capsys.readouterr().out


def test_bad_durations_value_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    status = main(["--durations=-1"])
    assert status == 4
    assert "--durations" in capsys.readouterr().err


def test_main_report_json_writes_one_record_per_test(project: Project, tmp_path: Path) -> None:
    """`--report-json` is `main`'s machine-readable alternative to the terminal reporter: one
    record per test, carrying exactly what a consumer can't get from `-v` today -- outcome,
    duration and a failure reason, read as data rather than parsed back out of printed text."""
    project.write(
        "test_sample.py",
        "async def test_pass():\n    pass\n\n"
        "async def test_fail():\n    x = 1\n    assert x == 2\n",
    )
    out = tmp_path / "report.json"

    status = main([str(project.root), f"--report-json={out}"])

    assert status == 1
    report = json.loads(out.read_text())
    assert report["runner"] == "velox"
    assert report["exit_status"] == 1
    assert report["collection_errors"] == []
    tests = {entry["id"]: entry for entry in report["tests"]}
    assert set(tests) == {"test_sample.py::test_pass", "test_sample.py::test_fail"}
    passed = tests["test_sample.py::test_pass"]
    assert passed["outcome"] == "passed"
    assert passed["failure_reason"] is None
    assert isinstance(passed["duration"], float)
    failed = tests["test_sample.py::test_fail"]
    assert failed["outcome"] == "failed"
    assert "AssertionError" in (failed["failure_reason"] or "")


def test_main_report_json_includes_skip_marked_tests(project: Project, tmp_path: Path) -> None:
    """A `skip`-marked test never reaches `run_suite` at all (collection keeps it out of
    `records`), so without this it would be invisible to a `--report-json` consumer entirely --
    not merely missing a duration, but absent from the file."""
    project.write(
        "test_sample.py",
        "import velox\n\n@velox.skip('not ready')\nasync def test_skipped():\n    pass\n",
    )
    out = tmp_path / "report.json"

    status = main([str(project.root), f"--report-json={out}"])

    assert status == 0
    report = json.loads(out.read_text())
    (entry,) = report["tests"]
    assert entry["id"] == "test_sample.py::test_skipped"
    assert entry["outcome"] == "skipped"
    assert entry["failure_reason"] == "not ready"


def test_main_report_json_includes_collection_errors(project: Project, tmp_path: Path) -> None:
    project.write("test_broken.py", "raise RuntimeError('boom')\n")
    out = tmp_path / "report.json"

    status = main([str(project.root), f"--report-json={out}"])

    assert status == 1
    report = json.loads(out.read_text())
    assert report["collection_errors"] == ["test_broken.py"]
    assert report["tests"] == []


def test_main_report_json_not_written_for_collect_only(project: Project, tmp_path: Path) -> None:
    """`--collect-only` never runs anything, so there is no run to report on."""
    project.write_passing_test()
    out = tmp_path / "report.json"

    status = main([str(project.root), f"--report-json={out}", "--collect-only"])

    assert status == 0
    assert not out.exists()


def test_main_report_json_creates_parent_directories(project: Project, tmp_path: Path) -> None:
    project.write_passing_test()
    out = tmp_path / "nested" / "dir" / "report.json"

    status = main([str(project.root), f"--report-json={out}"])

    assert status == 0
    assert out.is_file()


def test_main_runs_class_grouped_tests_end_to_end(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_sample.py",
        "import velox\n"
        "from velox import Depends\n\n"
        "@velox.fixture()\n"
        "async def number() -> int:\n    return 7\n\n"
        "class TestGroup:\n"
        "    async def test_injected(self, n: int = Depends(number)):\n"
        "        assert n == 7\n\n"
        "    async def test_fails(self):\n"
        "        assert False\n",
    )

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 1
    assert "1 failed" in out
    assert "2 tests · 1 passed" in out
    assert "test_sample.py::TestGroup::test_fails" in out


# Runtime safety at the CLI: the loop watchdog's flag, and what a Ctrl-C leaves behind.
# ------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["--loop-watchdog=-1", "--loop-watchdog=inf"])
def test_bad_loop_watchdog_value_is_a_usage_error(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main([bad])

    assert status == 4
    assert "--loop-watchdog" in capsys.readouterr().err


def test_loop_watchdog_zero_switches_the_watchdog_off(project: Project) -> None:
    """0 is a real value, not a rejected one: it is how the diagnostic is turned off."""
    project.write_passing_test()

    assert main([str(project.root), "--loop-watchdog=0"]) == 0


def test_a_bad_configured_loop_watchdog_names_the_config_file(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    chdir_project.write_pyproject("[tool.velox]\nloop_watchdog = -3\n")
    chdir_project.write_passing_test()

    status = main([])

    assert status == 4
    assert "pyproject.toml" in capsys.readouterr().err


def test_maxfail_reports_the_tests_it_cancelled(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run stops without waiting out the slow test, and says so rather than quietly
    leaving it out of the counts."""
    project.write(
        "test_sample.py",
        "import asyncio\n\n"
        "async def test_slow():\n    await asyncio.sleep(30)\n\n"
        "async def test_fails():\n    assert False\n",
    )

    status = main([str(project.root), "-x"])

    out = capsys.readouterr().out
    assert status == 1
    assert "1 failed · 1 cancelled" in out


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="velox only takes SIGINT on the main thread",
)
def test_a_ctrl_c_exits_two_and_still_prints_the_report(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit code 2 rather than whatever the finished tests added up to: an interrupted run
    never reached a verdict, and a Ctrl-C reading as success in CI would be worse than
    useless. The signal is sent from inside a test body, so it can only land while velox's own
    handler is installed."""
    project.write(
        "test_sample.py",
        "import asyncio, os, signal\n\n"
        "async def test_ok():\n    pass\n\n"
        "async def test_interrupts():\n"
        "    os.kill(os.getpid(), signal.SIGINT)\n"
        "    await asyncio.sleep(30)\n\n"
        "async def test_slow():\n    await asyncio.sleep(30)\n",
    )

    status = main([str(project.root), "--concurrency=3"])

    out = capsys.readouterr().out
    assert status == 2
    assert "INTERRUPTED (Ctrl-C)" in out
    assert "2 cancelled" in out


# ------------------------------------------------------------------------------------------
# Warning filters
# ------------------------------------------------------------------------------------------

_WARNING_SUITE = (
    "import warnings\n\n"
    "async def test_warns():\n"
    "    warnings.warn('legacy call', DeprecationWarning, stacklevel=1)\n"
)


def test_a_warning_is_reported_without_failing_the_test(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write("test_sample.py", _WARNING_SUITE)

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 0
    assert "--- warnings summary (1) ---" in out
    assert "DeprecationWarning: legacy call" in out
    assert "1 warning" in out


def test_dash_w_ignore_silences_a_warning(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write("test_sample.py", _WARNING_SUITE)

    status = main([str(project.root), "-W", "ignore::DeprecationWarning"])

    out = capsys.readouterr().out
    assert status == 0
    assert "warnings summary" not in out


def test_dash_w_error_fails_the_test_that_raised_the_warning(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write("test_sample.py", _WARNING_SUITE)

    status = main([str(project.root), "-W", "error::DeprecationWarning"])

    out = capsys.readouterr().out
    assert status == 1
    assert "DeprecationWarning: legacy call" in out


def test_dash_w_outranks_configured_filterwarnings(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    chdir_project.write_pyproject("[tool.velox]\nfilterwarnings = ['ignore::DeprecationWarning']\n")
    chdir_project.write("test_sample.py", _WARNING_SUITE)

    status = main(["-W", "error::DeprecationWarning"])

    assert status == 1
    assert "DeprecationWarning: legacy call" in capsys.readouterr().out


def test_a_filterwarnings_mark_outranks_both(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    chdir_project.write_pyproject("[tool.velox]\nfilterwarnings = ['error::DeprecationWarning']\n")
    chdir_project.write(
        "test_sample.py",
        "import warnings\n\nimport velox\n\n"
        "@velox.filterwarnings('ignore::DeprecationWarning')\n"
        "async def test_warns():\n"
        "    warnings.warn('legacy call', DeprecationWarning, stacklevel=1)\n",
    )

    status = main(["-W", "error::DeprecationWarning"])

    out = capsys.readouterr().out
    assert status == 0
    assert "warnings summary" not in out


def test_a_mark_filter_does_not_reach_a_concurrently_running_test(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of filtering per test rather than through `warnings.filters`: one
    test's `ignore` must not silence a sibling dispatched alongside it."""
    project.write(
        "test_sample.py",
        "import asyncio\nimport warnings\n\nimport velox\n\n"
        "@velox.filterwarnings('ignore::DeprecationWarning')\n"
        "async def test_silenced():\n"
        "    for _ in range(50):\n"
        "        warnings.warn('quiet', DeprecationWarning, stacklevel=1)\n"
        "        await asyncio.sleep(0)\n\n"
        "async def test_loud():\n"
        "    for _ in range(50):\n"
        "        warnings.warn('heard', DeprecationWarning, stacklevel=1)\n"
        "        await asyncio.sleep(0)\n",
    )

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 0
    assert "DeprecationWarning: heard" in out
    assert "quiet" not in out


def test_a_warning_raised_at_import_is_attributed_to_no_test(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_sample.py",
        "import warnings\n\n"
        "warnings.warn('at import', UserWarning, stacklevel=1)\n\n"
        "async def test_ok():\n    pass\n",
    )

    status = main([str(project.root)])

    out = capsys.readouterr().out
    assert status == 0
    assert "UserWarning: at import" in out
    assert "(no test running)" in out


def test_a_malformed_dash_w_is_a_usage_error(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_passing_test()

    status = main([str(project.root), "-W", "shout::UserWarning"])

    assert status == 4
    assert "unknown action" in capsys.readouterr().err


def test_the_json_report_carries_the_warnings_a_test_raised(project: Project) -> None:
    project.write("test_sample.py", _WARNING_SUITE)
    report = project.root / "report.json"

    main([str(project.root), "--report-json", str(report)])

    data = json.loads(report.read_text())
    (entry,) = data["tests"]
    (warning,) = entry["warnings"]
    assert warning["category"] == "DeprecationWarning"
    assert warning["message"] == "legacy call"
    assert warning["count"] == 1


def test_a_filter_can_name_a_warning_class_the_suite_defines(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Resolving a spec's category imports the module holding it, which for one of the suite's
    own only resolves once the rootdir is on `sys.path`."""
    chdir_project.write_pyproject(
        "[tool.velox]\nfilterwarnings = ['error::myapp.warnings.LegacyWarning']\n"
    )
    chdir_project.write("myapp/__init__.py", "")
    chdir_project.write("myapp/warnings.py", "class LegacyWarning(UserWarning): pass\n")
    chdir_project.write(
        "test_sample.py",
        "import warnings\n\nfrom myapp.warnings import LegacyWarning\n\n"
        "async def test_warns():\n"
        "    warnings.warn('legacy', LegacyWarning, stacklevel=1)\n",
    )

    status = main([])

    assert status == 1
    assert "LegacyWarning: legacy" in capsys.readouterr().out


def test_a_malformed_configured_filter_names_the_config_file(
    chdir_project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    chdir_project.write_pyproject("[tool.velox]\nfilterwarnings = ['shout::UserWarning']\n")
    chdir_project.write_passing_test()

    status = main([])

    err = capsys.readouterr().err
    assert status == 4
    assert "pyproject.toml" in err
    assert "unknown action" in err


def test_collect_only_still_reports_what_collection_warned_about(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_sample.py",
        "import warnings\n\n"
        "warnings.warn('at import', UserWarning, stacklevel=1)\n\n"
        "async def test_ok():\n    pass\n",
    )

    status = main([str(project.root), "--collect-only"])

    out = capsys.readouterr().out
    assert status == 0
    assert "UserWarning: at import" in out


def _failing_and_passing(project: Project) -> None:
    """Two files, one of which holds the only failure in the project."""
    project.write(
        "test_a.py",
        "async def test_ok_a():\n    pass\n\nasync def test_bad_a():\n    assert 1 == 2\n",
    )
    project.write("test_b.py", "async def test_ok_b():\n    pass\n")


def test_last_failed_reruns_only_the_failure(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _failing_and_passing(project)
    assert main([str(project.root)]) == 1
    capsys.readouterr()

    status = main([str(project.root), "--lf"])

    out = capsys.readouterr().out
    assert status == 1
    assert "1 test · " in out
    assert "test_a.py::test_bad_a" in out


def test_last_failed_does_not_import_a_file_holding_no_failure(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not importing what it has no intention of running is where --lf's speed comes from,
    so the marker a passing file writes at import time must not appear the second time."""
    _failing_and_passing(project)
    marker = project.root / "imported"
    project.write(
        "test_b.py",
        f"from pathlib import Path\n\nPath({str(marker)!r}).touch()\n\n"
        "async def test_ok_b():\n    pass\n",
    )
    assert main([str(project.root)]) == 1
    marker.unlink()
    capsys.readouterr()

    assert main([str(project.root), "--lf"]) == 1
    assert not marker.exists()


def test_last_failed_with_nothing_recorded_runs_the_whole_suite(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A first run, or one that went green, must not read as "no tests to run"."""
    project.write_passing_test()

    status = main([str(project.root), "--lf"])

    out = capsys.readouterr().out
    assert status == 0
    assert "--lf: nothing recorded" in out


def test_last_failed_forgets_a_test_that_now_passes(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _failing_and_passing(project)
    assert main([str(project.root)]) == 1
    project.write(
        "test_a.py",
        "async def test_ok_a():\n    pass\n\nasync def test_bad_a():\n    pass\n",
    )
    assert main([str(project.root), "--lf"]) == 0
    capsys.readouterr()

    status = main([str(project.root), "--lf"])

    out = capsys.readouterr().out
    assert status == 0
    assert "--lf: nothing recorded" in out
    assert "3 tests" in out


def test_last_failed_keeps_a_failure_a_narrower_run_never_reached(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Running one file must not erase what the run before it found in another."""
    _failing_and_passing(project)
    assert main([str(project.root)]) == 1
    assert main([str(project.root / "test_b.py")]) == 0
    capsys.readouterr()

    status = main([str(project.root), "--lf"])

    assert status == 1
    assert "test_a.py::test_bad_a" in capsys.readouterr().out


def test_last_failed_replays_a_file_that_failed_to_collect(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken import contributed no ids, so the whole file is what --lf has to replay."""
    project.write_passing_test()
    project.write("test_broken.py", "import nosuchmodule\n\nasync def test_x():\n    pass\n")
    assert main([str(project.root)]) == 1
    project.write("test_broken.py", "async def test_x():\n    pass\n")
    capsys.readouterr()

    status = main([str(project.root), "--lf"])

    out = capsys.readouterr().out
    assert status == 0
    assert "1 test · " in out
    assert "test_broken.py" in out


def test_failed_first_runs_everything_with_the_failure_leading(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _failing_and_passing(project)
    assert main([str(project.root)]) == 1
    capsys.readouterr()

    status = main([str(project.root), "--ff", "-v"])

    out = capsys.readouterr().out
    assert status == 1
    assert "3 tests" in out
    assert _lines_starting_with(out, "FAILED", "PASSED")[0].startswith("FAILED")


def test_last_failed_narrows_a_keyword_selection_rather_than_replacing_it(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write(
        "test_a.py",
        "async def test_bad_one():\n    assert 1 == 2\n\n"
        "async def test_bad_two():\n    assert 1 == 2\n",
    )
    assert main([str(project.root)]) == 1
    capsys.readouterr()

    status = main([str(project.root), "--lf", "-k", "one"])

    out = capsys.readouterr().out
    assert status == 1
    assert "1 test · " in out
    assert "test_bad_two" not in _lines_starting_with(out, "FAILED")[0]


def test_last_failed_and_failed_first_together_are_a_usage_error(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main([str(project.root), "--lf", "--ff"])

    assert status == 4
    assert "--lf" in capsys.readouterr().err


def test_collect_only_lists_what_last_failed_selected(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    _failing_and_passing(project)
    assert main([str(project.root)]) == 1
    capsys.readouterr()

    status = main([str(project.root), "--lf", "--collect-only"])

    out = capsys.readouterr().out
    assert status == 0
    assert "test_a.py::test_bad_a" in out
    assert "test_ok_b" not in out


def test_collect_only_leaves_the_cache_alone(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing ran, so there is nothing new to say about what failed."""
    _failing_and_passing(project)
    assert main([str(project.root)]) == 1
    assert main([str(project.root), "--collect-only"]) == 0
    capsys.readouterr()

    assert main([str(project.root), "--lf"]) == 1
    assert "1 test · " in capsys.readouterr().out


def test_maxfail_keeps_the_failures_it_stopped_short_of(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """`velox -x --lf` is the loop --lf exists for, so a run cut short must not report that
    every test it never reached has stopped failing."""
    project.write(
        "test_a.py",
        "async def test_bad_one():\n    assert 1 == 2\n\n"
        "async def test_bad_two():\n    assert 1 == 2\n",
    )
    assert main([str(project.root), "--serial"]) == 1
    assert main([str(project.root), "--serial", "-x", "--lf"]) == 1
    capsys.readouterr()

    status = main([str(project.root), "--serial", "--lf"])

    out = capsys.readouterr().out
    assert status == 1
    assert "2 tests" in out


def test_last_failed_forgets_a_test_that_no_longer_exists(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A renamed or deleted failure has no run left to settle it, so collection's own answer
    about what the file holds is what takes it out -- otherwise the cache never empties and
    every later --lf narrows to a file it selects nothing from."""
    project.write("test_a.py", "async def test_old():\n    assert 1 == 2\n")
    assert main([str(project.root)]) == 1
    project.write("test_a.py", "async def test_new():\n    pass\n")
    assert main([str(project.root)]) == 0
    capsys.readouterr()

    status = main([str(project.root), "--lf"])

    out = capsys.readouterr().out
    assert status == 0
    assert "--lf: nothing recorded" in out


def test_last_failed_keeps_a_test_a_keyword_expression_left_out(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Deselected is not gone: the run that skipped past a failure must not report it fixed."""
    project.write(
        "test_a.py",
        "async def test_bad():\n    assert 1 == 2\n\nasync def test_ok():\n    pass\n",
    )
    assert main([str(project.root)]) == 1
    assert main([str(project.root), "-k", "ok"]) == 0
    capsys.readouterr()

    assert main([str(project.root), "--lf"]) == 1
    assert "test_a.py::test_bad" in capsys.readouterr().out


def test_last_failed_replays_a_package_whose_init_failed_to_import(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A package `__init__.py` is never itself a discovered test file, so the tree below it is
    what --lf has to replay -- and what fixing it has to settle."""
    project.write("pkg/__init__.py", "import nosuchmodule\n")
    project.write("pkg/test_a.py", "async def test_x():\n    pass\n")
    project.write_passing_test()
    assert main([str(project.root)]) == 1
    project.write("pkg/__init__.py", "")
    capsys.readouterr()

    status = main([str(project.root), "--lf"])

    out = capsys.readouterr().out
    assert status == 0
    assert "1 test · " in out
    assert "pkg/test_a.py" in out

    capsys.readouterr()
    assert main([str(project.root), "--lf"]) == 0
    assert "--lf: nothing recorded" in capsys.readouterr().out


def test_last_failed_selecting_nothing_does_not_exit_zero(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A skip-marked sibling in the candidate file must not count as "something was collected",
    which would report a --lf that executed nothing as a green run."""
    project.write(
        "test_a.py",
        "import velox\n\nasync def test_bad():\n    assert 1 == 2\n\n"
        "@velox.skip('later')\nasync def test_skipped():\n    pass\n",
    )
    assert main([str(project.root)]) == 1
    project.write(
        "test_a.py",
        "import velox\n\nasync def test_renamed():\n    assert 1 == 2\n\n"
        "@velox.skip('later')\nasync def test_skipped():\n    pass\n",
    )
    capsys.readouterr()

    status = main([str(project.root), "--lf"])

    assert status == 5
    assert "1 skipped" not in capsys.readouterr().out


def test_last_failed_settles_a_recorded_test_whose_file_was_deleted(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deleted file is never handed to collection again, so no import can settle what it
    recorded. Left in the cache the entry outlives the suite: every later --lf narrows to a file
    that isn't there, selects nothing and exits 5."""
    project.write("test_gone.py", "async def test_bad():\n    assert 1 == 2\n")
    project.write_passing_test()
    assert main([str(project.root)]) == 1
    (project.root / "test_gone.py").unlink()
    capsys.readouterr()

    assert main([str(project.root)]) == 0
    capsys.readouterr()

    assert main([str(project.root), "--lf"]) == 0
    assert "--lf: nothing recorded" in capsys.readouterr().out


def test_a_run_over_one_directory_keeps_failures_recorded_elsewhere(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The counterpart to the deletion above: "gone" is read off the filesystem, not off what
    this run happened to discover, so a narrower run must not declare the rest of the suite's
    files vanished."""
    project.write("one/test_a.py", "async def test_bad():\n    assert 1 == 2\n")
    project.write("two/test_b.py", "async def test_ok():\n    pass\n")
    assert main([str(project.root)]) == 1
    assert main([str(project.root / "two")]) == 0
    capsys.readouterr()

    assert main([str(project.root), "--lf"]) == 1
    assert "one/test_a.py::test_bad" in capsys.readouterr().out.replace(os.sep, "/")


def test_a_misplaced_declaration_is_not_recorded_as_an_error_file(project: Project) -> None:
    """`velox.use(...)` in a module velox never collects is reported by every run that imports
    it, and named by a path discovery does not produce -- an absolute one, or a bare module name.
    Recording it would put a string in the cache no run could settle."""
    project.write(
        "misplaced_helper.py",
        "import velox\n\n"
        "@velox.fixture()\nasync def thing() -> int:\n    return 1\n\n"
        "velox.use(thing)\n",
    )
    project.write("test_a.py", "import misplaced_helper\n\nasync def test_ok():\n    pass\n")

    try:
        assert main([str(project.root)]) == 1
    finally:
        # The scan reads sys.modules, and main() leaves what the suite imported behind: another
        # test's run would find this one's helper there and report it against its own project.
        sys.modules.pop("misplaced_helper", None)

    recorded = json.loads((project.root / ".velox_cache" / "lastfailed.json").read_text())
    assert recorded["error_files"] == []


def test_last_failed_settles_the_cases_of_a_test_that_became_skipped(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A --lf run deselects the skip it wasn't asked for, and a deselected unexpanded test
    normally means "this run never built its cases". It must not read that way here, or the
    recorded `[case]` ids stay in the cache and every later --lf exits 5."""
    project.write(
        "test_a.py",
        "import velox\n\n"
        "@velox.parametrize('value', [1, 2])\n"
        "async def test_role(value: int) -> None:\n    assert value == 0\n",
    )
    assert main([str(project.root)]) == 1
    project.write(
        "test_a.py",
        "import velox\n\n"
        "@velox.skip('later')\n"
        "@velox.parametrize('value', [1, 2])\n"
        "async def test_role(value: int) -> None:\n    assert value == 0\n",
    )
    capsys.readouterr()

    assert main([str(project.root), "--lf"]) == 5
    capsys.readouterr()

    assert main([str(project.root), "--lf"]) == 0
    assert "--lf: nothing recorded" in capsys.readouterr().out


def test_a_run_below_a_namespace_dir_does_not_clear_a_broken_package(
    project: Project,
) -> None:
    """Collecting under a package is what imports it, and a directory without an `__init__.py`
    ends that walk. A run that never imported `pkg/__init__.py` has no answer about it, so it
    must not report the recorded failure to import it fixed."""
    # Pins rootdir, so both runs below share the one cache rather than the narrower run
    # starting its own next to the directory it was pointed at.
    project.write_pyproject("[tool.velox]\n")
    project.write("pkg/__init__.py", "import nosuchmodule\n")
    project.write("pkg/test_b.py", "async def test_y():\n    pass\n")
    # No __init__.py of its own, so collecting it never imports the package above it.
    project.write("pkg/sub/test_a.py", "async def test_x():\n    pass\n")
    assert main([str(project.root)]) == 1

    assert main([str(project.root / "pkg" / "sub")]) == 0

    recorded = json.loads((project.root / ".velox_cache" / "lastfailed.json").read_text())
    assert recorded["error_files"] == ["pkg/__init__.py"]


def test_last_failed_does_not_invent_a_misplaced_declaration(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """--lf narrows which files are collected, not which ones are test modules: a
    `velox.use(...)` in a test module left out of the run is still a declaration in a test
    module, however the run reaches that module."""
    project.write(
        "test_shared.py",
        "import velox\n\n"
        "@velox.fixture()\nasync def thing() -> int:\n    return 1\n\n"
        "velox.use(thing)\n\n"
        "async def test_shared_ok():\n    pass\n",
    )
    project.write(
        "test_a.py",
        "import test_shared\n\nasync def test_bad():\n    assert 1 == 2\n",
    )
    try:
        assert main([str(project.root)]) == 1
        project.write(
            "test_a.py",
            "import test_shared\n\nasync def test_bad():\n    pass\n",
        )
        capsys.readouterr()

        assert main([str(project.root), "--lf"]) == 0
    finally:
        sys.modules.pop("test_shared", None)
    assert "COLLECTION ERROR" not in capsys.readouterr().out


def test_a_broken_package_does_not_settle_the_failures_below_it(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file under a package that failed to import is skipped before it is read, and carries no
    error of its own -- so it must not read as "collected, and holding nothing", which would
    drop the failures recorded in it."""
    project.write("pkg/__init__.py", "")
    project.write("pkg/test_a.py", "async def test_bad():\n    assert 1 == 2\n")
    assert main([str(project.root)]) == 1
    project.write("pkg/__init__.py", "import nosuchmodule\n")
    assert main([str(project.root)]) == 1

    recorded = json.loads((project.root / ".velox_cache" / "lastfailed.json").read_text())
    assert "pkg/test_a.py::test_bad" in recorded["failed"]


def test_a_package_that_loses_its_last_test_stops_being_recorded(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing is left to collect under the package, so no import can settle it and the file is
    still on disk for `missing_paths` to find. Discovery looking and finding nothing is the only
    answer available -- without it every later --lf narrows to an empty tree and exits 5."""
    project.write("pkg/__init__.py", "import nosuchmodule\n")
    project.write("pkg/test_a.py", "async def test_x():\n    pass\n")
    project.write_passing_test()
    assert main([str(project.root)]) == 1
    (project.root / "pkg" / "test_a.py").unlink()

    assert main([str(project.root)]) == 0
    capsys.readouterr()

    assert main([str(project.root), "--lf"]) == 0
    assert "--lf: nothing recorded" in capsys.readouterr().out


def test_a_run_inside_a_package_does_not_declare_it_empty(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """Walking a test-free directory *within* a broken package is not evidence the package holds
    no test -- one sits right beside it."""
    project.write_pyproject("[tool.velox]\n")
    project.write("pkg/__init__.py", "import nosuchmodule\n")
    project.write("pkg/other/__init__.py", "")
    project.write("pkg/other/test_x.py", "async def test_x():\n    pass\n")
    (project.root / "pkg" / "sub").mkdir(parents=True)
    assert main([str(project.root)]) == 1

    assert main([str(project.root / "pkg" / "sub")]) == 5
    capsys.readouterr()

    recorded = json.loads((project.root / ".velox_cache" / "lastfailed.json").read_text())
    assert recorded["error_files"] == ["pkg/__init__.py"]


def test_a_package_reached_only_through_a_namespace_dir_stops_being_recorded(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """A test under `pkg/sub/` with no `__init__.py` of its own never imports `pkg/__init__.py`,
    so collecting it can never settle the recorded failure to import it. Being unreachable is
    what makes the entry dead."""
    project.write("pkg/__init__.py", "import nosuchmodule\n")
    project.write("pkg/test_a.py", "async def test_x():\n    pass\n")
    project.write_passing_test()
    assert main([str(project.root)]) == 1
    # Only a namespace directory holds tests under the package now.
    (project.root / "pkg" / "test_a.py").unlink()
    project.write("pkg/sub/test_b.py", "async def test_y():\n    pass\n")

    assert main([str(project.root)]) == 0
    capsys.readouterr()

    assert main([str(project.root), "--lf"]) == 0
    assert "--lf: nothing recorded" in capsys.readouterr().out


def test_a_recorded_test_in_a_newly_ignored_directory_stops_being_recorded(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The file is still on disk, so nothing about the filesystem says the entry is dead. This
    run walked past it and no longer discovers it, which does."""
    project.write("legacy/test_a.py", "async def test_bad():\n    assert 1 == 2\n")
    project.write_passing_test()
    assert main([str(project.root)]) == 1
    project.write_pyproject('[tool.velox]\nignore = ["legacy"]\n')

    assert main([str(project.root)]) == 0
    capsys.readouterr()

    assert main([str(project.root), "--lf"]) == 0
    assert "--lf: nothing recorded" in capsys.readouterr().out


def test_one_malformed_test_does_not_strand_a_renamed_sibling(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The file collected fine except for the one bad test, so velox knows its ids in full: a
    recorded failure it no longer holds is settled like any other."""
    project.write(
        "test_a.py",
        "async def test_bad():\n    assert 1 == 2\n\nasync def test_ok():\n    pass\n",
    )
    assert main([str(project.root)]) == 1
    project.write(
        "test_a.py",
        "def _impl():\n    pass\n\ntest_alias = _impl\n\nasync def test_renamed():\n    pass\n",
    )
    assert main([str(project.root)]) == 1
    capsys.readouterr()

    recorded = json.loads((project.root / ".velox_cache" / "lastfailed.json").read_text())
    assert recorded["failed"] == []


def test_last_failed_says_when_no_recorded_failure_is_in_the_selection(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The recorded failure is real and this run is right not to settle it, but "0 tests" and
    exit 5 on their own read as a suite that collected nothing."""
    # Pins rootdir, so the narrower run reads the cache the first one wrote.
    project.write_pyproject("[tool.velox]\n")
    project.write("one/test_a.py", "async def test_bad():\n    assert 1 == 2\n")
    project.write("two/test_b.py", "async def test_ok():\n    pass\n")
    assert main([str(project.root)]) == 1
    capsys.readouterr()

    status = main([str(project.root / "two"), "--lf"])

    assert status == 5
    assert "--lf: no recorded failure is in this run's selection" in capsys.readouterr().out


def test_collect_only_leaves_the_cache_directory_gitignored(project: Project) -> None:
    """The rewriter fills the same directory during collection, so a project whose only velox
    invocation is --collect-only must not pick up an untracked one."""
    project.write_passing_test()

    assert main([str(project.root), "--collect-only"]) == 0

    gitignore = project.root / ".velox_cache" / ".gitignore"
    assert gitignore.exists()
    assert gitignore.read_text().endswith("*\n")
