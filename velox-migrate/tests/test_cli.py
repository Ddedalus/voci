"""Tests for velox_migrate.cli: argument handling and the exit codes `extract` reports."""

from __future__ import annotations

from pathlib import Path

import pytest
from velox_migrate import cli, schema

SUITE = Path(__file__).resolve().parents[1] / "corpus" / "fixtures_showcase"


def test_extract_writes_a_loadable_dump(tmp_path: Path) -> None:
    out = tmp_path / "ground-truth.json"

    code = cli.main(["extract", str(SUITE), "-o", str(out)])

    assert code == 0
    assert len(schema.load(out)["items"]) == 10


def test_arguments_after_a_double_dash_reach_pytest(tmp_path: Path) -> None:
    # argparse leaves the separator in the leftovers, and pytest reads a bare `--` as "the rest
    # are file paths" — which would silently collect nothing.
    out = tmp_path / "ground-truth.json"

    code = cli.main(["extract", str(SUITE), "-o", str(out), "--", "-p", "no:cacheprovider"])

    assert code == 0
    assert len(schema.load(out)["items"]) == 10


def test_unrecognized_arguments_reach_pytest(tmp_path: Path) -> None:
    out = tmp_path / "ground-truth.json"

    code = cli.main(["extract", str(SUITE), "-o", str(out), "--ignore", str(SUITE / "integration")])

    assert code == 0
    assert len(schema.load(out)["items"]) == 8


def test_a_suite_that_cannot_be_collected_reports_pytests_own_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["extract", str(tmp_path / "nonexistent"), "-o", str(tmp_path / "out.json")])

    assert code != 0
    assert "Fix collection first" in capsys.readouterr().err


def test_no_command_prints_help_rather_than_failing_obscurely(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main([])

    assert code == 2
    assert "extract" in capsys.readouterr().out
