"""Placeholder tests for the velox CLI scaffold.

Real collection/scheduling/reporting tests land alongside those features;
this just proves the package imports and the entrypoint is wired up.
"""

import pytest
from velox import __version__
from velox.cli import build_parser, main


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
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 1
