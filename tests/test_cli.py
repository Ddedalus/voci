"""Placeholder tests for the velox CLI scaffold.

Real collection/scheduling/reporting tests land alongside those features;
this just proves the package imports and the entrypoint is wired up.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from velox import __version__
from velox.cli import _default_test_roots, build_parser, main


def test_version_is_a_string() -> None:
    assert isinstance(__version__, str)
    assert __version__


def test_build_parser_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_main_with_one_broken_module_and_nothing_else_exits_one(tmp_path: Path) -> None:
    """`main` returns its status rather than raising `SystemExit` (that mechanism belongs to the
    `if __name__ == "__main__": sys.exit(main())` block), so a caller invoking it directly — like
    this test — must still see it.

    Hermetic version of "one collection error, zero records ⇒ exit 1" (spec/02 §4). An earlier
    version of this test pointed `main([])` at this repo's own `tests/` dir instead: that
    re-imported and *executed* every module under it a second time, under `velox_tests.*` names
    that stayed in `sys.modules` afterwards (module-level side effects — hook installation in the
    assertion tests, fixture state — running twice, in an order pytest doesn't control), was
    cwd-dependent, and its `== 1` assertion silently re-encoded today's contents of `tests/`:
    adding one `async def test_*` anywhere in this repo's suite would have turned it red for
    reasons unrelated to the CLI.
    """
    (tmp_path / "test_broken.py").write_text("raise RuntimeError('boom')\n")
    assert main([str(tmp_path)]) == 1


def test_main_all_passing_exits_zero(tmp_path: Path) -> None:
    """The exit code every CI green build actually depends on — untested through `main` before
    this (only `_run.exit_code_for` was tested in isolation, which never exercises the
    discover -> collect -> run wiring that decides what it's called with)."""
    (tmp_path / "test_ok.py").write_text("async def test_ok():\n    pass\n")
    assert main([str(tmp_path)]) == 0


def test_main_empty_directory_exits_five(tmp_path: Path) -> None:
    assert main([str(tmp_path)]) == 5


def test_main_rejects_a_nonexistent_path_as_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd path and a genuinely empty suite must not look the same (I8) — both used to
    silently walk to nothing and exit 5."""
    missing = tmp_path / "does_not_exist"
    status = main([str(missing)])
    assert status == 4
    assert str(missing) in capsys.readouterr().err


def test_main_rejects_a_test_id_argument_as_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """spec/02 §1 documents `path.py::test_name` as supported invocation syntax; M0 doesn't
    parse it yet and must say so rather than fail as a missing path."""
    status = main(["tests/test_run.py::test_x"])
    assert status == 4
    assert "test ids" in capsys.readouterr().err


def test_default_roots_prefer_tests_dir_over_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    monkeypatch.chdir(tmp_path)
    assert _default_test_roots() == [Path("tests")]


def test_default_roots_fall_back_to_cwd_without_a_tests_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _default_test_roots() == [Path()]


def test_rewrite_cache_with_plain_mode_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`plan` never resolves `--rewrite-cache` in `plain` mode, so passing both used to be a
    silently-ignored contradiction instead of an error the user could act on."""
    status = main(["--assert=plain", "--rewrite-cache=/tmp/wherever"])
    assert status == 4
    assert "--rewrite-cache" in capsys.readouterr().err


def test_main_runs_a_passing_and_a_failing_async_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end M0 slice: discover -> import -> run -> print -> exit code (spec/00 §8)."""
    (tmp_path / "test_sample.py").write_text(
        "async def test_pass():\n"
        "    assert 1 + 1 == 2\n"
        "\n"
        "async def test_fail():\n"
        "    x = 2\n"
        "    y = 3\n"
        "    assert x == y\n"
    )

    status = main([str(tmp_path)])

    out = capsys.readouterr().out
    assert status == 1  # one failure present
    assert "test_pass PASSED" in out
    assert "test_fail FAILED" in out
    # A bare, un-rewritten `assert` also raises `AssertionError` — asserting only that string
    # would pass even if the rewrite hook were never actually consulted (as it in fact wasn't,
    # for a while: `_collect._import_module` used to import via `spec_from_file_location`
    # alone, which never consults `sys.meta_path`, silently defeating `cli.main`'s
    # `_rewrite.install` call despite the `assertions: rewrite` header line). Assert on the
    # introspection text instead — `"assert 2 == 3"` is only ever produced by the AST rewrite.
    assert "assert 2 == 3" in out


def test_main_reports_a_setup_failure_as_error_not_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """M1: a fixture that raises during setup produces `Outcome.ERROR`, distinct from `FAILED` —
    end to end through `main`, this covers both the per-test line (`ERROR`, not `FAILED`) and the
    summary's `errored` bucket, neither of which anything exercised before this."""
    (tmp_path / "test_sample.py").write_text(
        "import velox\n\n"
        "@velox.fixture()\n"
        "def broken():\n"
        "    raise RuntimeError('setup boom')\n\n"
        "async def test_needs_it(value: int = velox.Depends(broken)):\n"
        "    pass\n"
    )

    status = main([str(tmp_path)])

    out = capsys.readouterr().out
    assert status == 1
    assert "test_needs_it ERROR" in out
    assert "setup boom" in out
    assert "1 errored" in out


def test_main_reports_a_skipped_test_and_still_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `@velox.skip`-marked test must neither run for real nor fail the build over being
    skipped (spec/05 §4: `skipped` contributes `0` to the exit code, same as `passed`)."""
    (tmp_path / "test_sample.py").write_text(
        "import velox\n\n"
        "@velox.skip('not ready')\n"
        "async def test_skipped():\n"
        "    raise AssertionError('must not run')\n"
    )

    status = main([str(tmp_path)])

    out = capsys.readouterr().out
    assert status == 0
    assert "test_skipped SKIPPED (not ready)" in out
