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
    # M1 reporter slice: the flat "id OUTCOME" dump is gone, replaced by `_report.Reporter`'s
    # per-file block (spec/10 §2) plus the failure-details/short-summary sections `finish` builds
    # (see `test_report.py` for the reporter's own dedicated coverage) -- both tests live in one
    # file, so the block is marked FAIL with a "(1 failed)" count rather than a per-test PASS line.
    # Review (test quality): `"FAIL" in out` no longer means what the comment above says. "FAIL"
    # is a substring of "FAILED", which the failure-details header and the short-summary line both
    # print, so this assertion is satisfied even if no per-file block were emitted at all — the
    # only thing actually pinning the block is `"(1 failed)"` on the next line. The old assertions
    # this replaced (`"test_pass PASSED"`, `"test_fail FAILED"`) paired an id with an outcome; the
    # new ones are three independent substrings that never have to appear together, and nothing
    # here asserts the passing test is reported anywhere at all any more. Matching the block line
    # as a line — e.g. `any(line.startswith("FAIL ") and "test_sample.py" in line for line in
    # out.splitlines())` — restores what was lost, and would also have caught the `paths_by_id`
    # under-count described in `cli.py` (which prints `PASS` for a file that failed).
    assert "FAIL" in out
    assert "(1 failed)" in out
    assert "::test_fail" in out
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
    # M1 reporter slice: see the equivalent comment on
    # `test_main_runs_a_passing_and_a_failing_async_test` -- the failure-details section prints
    # "ERROR <id>", not "<id> ERROR".
    assert "ERROR" in out
    assert "::test_needs_it" in out
    assert "setup boom" in out
    assert "1 errored" in out


def test_bad_concurrency_value_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """M1 concurrency: `--concurrency` must be a positive integer -- `0`/negative is a usage
    error (exit 4), same style as the other checks in `main` (`_invalid_path_argument`, the
    `--rewrite-cache`/`--assert=plain` combo). Covers both sides of `main`'s `< 1` check, not just
    `0` -- `--concurrency=-4` also pins that the message interpolates the actual value given."""
    for bad in ("--concurrency=0", "--concurrency=-4"):
        status = main([bad])
        assert status == 4
        assert "--concurrency" in capsys.readouterr().err


def test_main_runs_end_to_end_with_a_custom_concurrency(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text("async def test_ok():\n    pass\n")
    assert main([str(tmp_path), "--concurrency=2"]) == 0


def test_bad_timeout_value_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """The `--timeout` sibling of `test_bad_concurrency_value_is_a_usage_error`: `0`/negative/
    non-finite are all rejected the same way, per `build_parser`'s comment on this flag (a `0` or
    negative "budget" would otherwise mean "fail every test that happens to suspend," not a
    useful, well-defined mode)."""
    for bad in ("--timeout=0", "--timeout=-1", "--timeout=nan", "--timeout=inf"):
        status = main([bad])
        assert status == 4
        assert "--timeout" in capsys.readouterr().err


def test_main_reports_a_timeout_as_its_own_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end `--timeout`: a hanging test surfaces `TIMEOUT`, distinct from `FAILED`/`ERROR`,
    in both the per-test line and the summary's now-explicit `timed out` bucket."""
    (tmp_path / "test_sample.py").write_text(
        "import asyncio\n\nasync def test_hangs():\n    await asyncio.sleep(10)\n"
    )

    status = main([str(tmp_path), "--timeout=0.05"])

    out = capsys.readouterr().out
    assert status == 1
    # M1 reporter slice: see the equivalent comment on
    # `test_main_runs_a_passing_and_a_failing_async_test`.
    assert "TIMEOUT" in out
    assert "::test_hangs" in out
    assert "1 timed out" in out


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


def test_main_prints_jest_style_per_file_blocks_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """M1 reporter slice (spec/10 §2), end to end through `main`: two files, one all-passing and
    one with a failure, produce two per-file scrollback blocks plus a failure-details/short-summary
    section and the wall-vs-Σ final line -- not the old flat "id OUTCOME" dump (see
    `tests/test_report.py` for the reporter's own unit coverage of every piece exercised here)."""
    (tmp_path / "test_a.py").write_text(
        "async def test_one():\n    pass\n\nasync def test_two():\n    pass\n"
    )
    (tmp_path / "test_b.py").write_text("async def test_broken():\n    assert 1 == 2\n")

    status = main([str(tmp_path)])

    out = capsys.readouterr().out
    assert status == 1
    # Review (test quality): this is the only end-to-end coverage the reporter has, and it is
    # eleven unanchored `in out` substring checks — none of which asserts that any two of them are
    # on the *same line*. `"PASS"`/`"test_a.py"` would both be satisfied by a run that printed a
    # `PASS` block for test_b.py and mentioned test_a.py only in a traceback path; `"FAIL"` is a
    # substring of the `FAILED` lines below it. The whole point of the block format is the
    # association `<status> <path> <count> tests <duration>`, and nothing here pins it. Splitting
    # `out` into lines once and asserting two block lines by prefix and content is three lines and
    # would catch the real defects this slice has: the `paths_by_id` under-count (prints `PASS ...
    # 1 tests` for a two-test file that failed) and the block-vs-summary contradiction it causes.
    #
    # Three properties this test's own docstring names but does not check, worth adding while it is
    # the only integration coverage: (a) that there are exactly *two* block lines, i.e. blocks are
    # per file and not per test — the regression this format exists to prevent; (b) that the
    # wall-vs-Σ line is actually final, which it is not (`main` prints the skip list, collection-
    # error tracebacks and its own `N tests: ...` summary after it — see the notes in `cli.py`);
    # and (c) the short-summary *reason*, which is where `_failure_reason` is wrong for every
    # non-trivial assert. `assert 1 == 2` was chosen here and it is precisely the one shape the
    # heuristic gets right; `assert 1 == 2, "widget count"` gives `- assert 1 == 2` and drops the
    # message, and that variant belongs in this test.
    #
    # Per-file blocks: one PASS (2 tests), one FAIL (1 test, 1 failed).
    assert "PASS" in out
    assert "test_a.py" in out
    assert "FAIL" in out
    assert "test_b.py" in out
    assert "(1 failed)" in out
    # Failure details + short test summary, in logical order.
    assert "--- short test summary ---" in out
    assert "FAILED" in out
    assert "::test_broken" in out
    assert "assert 1 == 2" in out
    # Wall-vs-Σ final line (spec/10 §2's proof-of-value metric).
    assert "tests ·" in out
    assert "wall (Σ" in out
