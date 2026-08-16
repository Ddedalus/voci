"""Tests for velox_migrate.cli: argument handling and the exit codes `extract` reports."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from velox_migrate import cli, extractor, schema

SUITE = Path(__file__).resolve().parents[1] / "corpus" / "fixtures_showcase"


def test_the_cli_names_the_same_default_output_as_the_plugin() -> None:
    # The extractor cannot import from this package — it is copied out of it — so the constant
    # exists in both on purpose.
    assert cli.DEFAULT_OUT == extractor.DEFAULT_OUT


def test_the_command_line_works_without_pytest_installed() -> None:
    # Only `extract` needs pytest, and it runs pytest as a subprocess in the suite's own
    # environment. Importing pytest here would make the whole tool need one too.
    blocked = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('pytest', '_pytest'):\n"
        "            raise ImportError(name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "import velox_migrate.cli, velox_migrate.model, velox_migrate.schema\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", blocked], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_extract_writes_a_loadable_dump(tmp_path: Path) -> None:
    out = tmp_path / "ground-truth.json"

    code = cli.main(["extract", str(SUITE), "-o", str(out)])

    assert code == 0
    assert len(schema.load(out)["items"]) == 11


def test_arguments_after_a_double_dash_reach_pytest(tmp_path: Path) -> None:
    out = tmp_path / "ground-truth.json"

    code = cli.main(["extract", str(SUITE), "-o", str(out), "--", "-p", "no:cacheprovider"])

    assert code == 0
    assert len(schema.load(out)["items"]) == 11


def test_a_pytest_option_keeps_its_value(tmp_path: Path) -> None:
    # The separator has to be honored before argparse runs: argparse claims the flags it knows
    # wherever they appear and files the rest as positionals, which would send `--ignore` and
    # the path it applies to to pytest as two unrelated arguments.
    out = tmp_path / "ground-truth.json"

    code = cli.main(
        ["extract", str(SUITE), "-o", str(out), "--", "--ignore", str(SUITE / "integration")]
    )

    assert code == 0
    assert len(schema.load(out)["items"]) == 9


def test_a_pytest_option_that_looks_like_one_of_ours_is_not_intercepted(tmp_path: Path) -> None:
    out = tmp_path / "ground-truth.json"

    code = cli.main(["extract", str(SUITE), "-o", str(out), "--", "-k", "test_engine"])

    assert code == 0
    assert len(schema.load(out)["items"]) == 1


def test_a_suite_that_cannot_be_collected_reports_pytests_own_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["extract", str(tmp_path / "nonexistent"), "-o", str(tmp_path / "out.json")])

    assert code != 0
    assert "without collecting this suite" in capsys.readouterr().err


def test_no_command_prints_help_rather_than_failing_obscurely(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main([])

    assert code == 2
    assert "extract" in capsys.readouterr().out
