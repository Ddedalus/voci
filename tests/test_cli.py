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


def test_main_not_yet_implemented() -> None:
    # `main` returns its status rather than raising `SystemExit` (that mechanism belongs to the
    # `if __name__ == "__main__": sys.exit(main())` block), so a caller invoking it directly —
    # like this test — must still see a failing status.
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
