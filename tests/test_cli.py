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


def test_main_with_no_paths_collects_this_repos_own_tests_dir() -> None:
    """`main` returns its status rather than raising `SystemExit` (that mechanism belongs to the
    `if __name__ == "__main__": sys.exit(main())` block), so a caller invoking it directly — like
    this test — must still see it.

    With no `PATHS`, `main` walks this repo's own `tests/` (`_default_test_roots`). Every file
    there is an ordinary pytest-style module — `def test_*`, not `async def test_*` — so M0
    collects zero records from all of them. `tests/assertion/test_explanations.py` also becomes
    a collection error: its `from .conftest import ...` needs package context that velox's
    flat, path-derived import names deliberately don't provide (spec/03 §3's traded-away
    `__init__.py`/`ImportPathMismatchError` machinery). One error, zero records: exit code 1
    (spec/02 §4), not 5 — "no tests collected" would be a lie about that error's existence.
    """
    assert main([]) == 1


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
        "    assert 1 + 1 == 3\n"
    )

    status = main([str(tmp_path)])

    out = capsys.readouterr().out
    assert status == 1  # one failure present
    assert "test_pass PASSED" in out
    assert "test_fail FAILED" in out
    assert "AssertionError" in out
