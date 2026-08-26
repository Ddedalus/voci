"""Tests for velox_migrate.cli: argument handling and the exit codes `extract` reports."""

from __future__ import annotations

import errno
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from velox_migrate import cli, extractor, schema

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
SUITE = CORPUS / "fixtures_showcase"
# A checked-in dump of SUITE, so the convert tests need no pytest run of their own.
DUMP = CORPUS / "dumps" / "fixtures_showcase-pytest-8.4.json"


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


def test_a_suite_narrowed_to_nothing_is_a_clean_result(tmp_path: Path) -> None:
    # Collecting no tests is what pytest reports for an empty suite and for a filter that matches
    # nothing. Neither is a failure to collect, and the dump the extractor writes is loadable.
    out = tmp_path / "ground-truth.json"

    code = cli.main(["extract", str(SUITE), "-o", str(out), "--", "-k", "matches_nothing_at_all"])

    assert code == 0
    assert schema.load(out)["items"] == []


def test_a_failed_extract_does_not_leave_the_previous_dump_readable(tmp_path: Path) -> None:
    # A dump that outlives the run that failed to replace it is the worst outcome available: it
    # describes the suite as it was, and nothing downstream can tell.
    out = tmp_path / "ground-truth.json"
    assert cli.main(["extract", str(SUITE), "-o", str(out)]) == 0
    assert len(schema.load(out)["items"]) == 11

    code = cli.main(["extract", str(tmp_path / "nonexistent"), "-o", str(out)])

    assert code != 0
    with pytest.raises(schema.DumpError):
        schema.load(out)


def test_no_command_prints_help_rather_than_failing_obscurely(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main([])

    assert code == 2
    assert "extract" in capsys.readouterr().out


def test_audit_writes_both_artifacts_beside_the_dump(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dump = tmp_path / "ground-truth.json"
    assert cli.main(["extract", str(SUITE), "-o", str(dump)]) == 0

    code = cli.main(["audit", "-d", str(dump), "-r", str(SUITE)])

    assert code == 0
    findings = json.loads((tmp_path / cli.FINDINGS_NAME).read_text(encoding="utf-8"))
    assert findings["totals"]["tests"] == 11
    assert (tmp_path / cli.REPORT_NAME).read_text(encoding="utf-8").startswith("# ")
    assert cli.REPORT_NAME in capsys.readouterr().out


def test_audit_writes_where_it_is_told_and_stays_quiet_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dump = tmp_path / "ground-truth.json"
    assert cli.main(["extract", str(SUITE), "-o", str(dump)]) == 0
    out = tmp_path / "elsewhere"

    code = cli.main(["audit", "-d", str(dump), "-r", str(SUITE), "-o", str(out), "--quiet"])

    assert code == 0
    assert (out / cli.FINDINGS_NAME).is_file()
    assert capsys.readouterr().out == ""


def test_auditing_without_a_dump_says_how_to_get_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["audit", "-d", str(tmp_path / "nothing.json")])

    assert code == 1
    assert "velox-migrate extract" in capsys.readouterr().err


def test_auditing_against_the_wrong_tree_refuses_rather_than_reporting_no_hazards(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A root holding none of the suite's files would produce an audit with no test bodies in it,
    # which reads as a clean suite.
    dump = tmp_path / "ground-truth.json"
    assert cli.main(["extract", str(SUITE), "-o", str(dump)]) == 0

    code = cli.main(["audit", "-d", str(dump), "-r", str(tmp_path)])

    assert code == 1
    assert "--root" in capsys.readouterr().err


class _ClosedPipe:
    """A stdout that fails every write, as the far end of a pipe does once its reader quits."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    def flush(self) -> None:
        pass


def _copy_of_showcase(tmp_path: Path, name: str) -> Path:
    suite = tmp_path / name
    shutil.copytree(SUITE, suite)
    return suite


def _tree(root: Path) -> dict[str, str]:
    """Every source file under `root`, keyed by its relative path."""
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def test_converting_without_write_says_so_and_leaves_the_tree_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    suite = _copy_of_showcase(tmp_path, "suite")
    before = _tree(suite)

    code = cli.main(["convert", "-d", str(DUMP), "-r", str(suite)])

    assert code == 0
    assert "nothing written; pass --write to apply" in capsys.readouterr().out
    assert _tree(suite) == before


def test_converting_with_write_rewrites_the_tree_and_counts_the_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    suite = _copy_of_showcase(tmp_path, "suite")
    before = _tree(suite)

    code = cli.main(["convert", "-d", str(DUMP), "-r", str(suite), "--write"])

    assert code == 0
    assert "file(s) under" in capsys.readouterr().out
    assert _tree(suite) != before


def test_a_reader_who_quits_part_way_through_the_diff_still_gets_the_whole_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reading the plan through a pager and quitting it kills this process on its next write. The
    # tree that is left behind has to be the converted one, not however much of it had been
    # printed before the pipe closed.
    read = _copy_of_showcase(tmp_path, "read")
    assert cli.main(["convert", "-d", str(DUMP), "-r", str(read), "--write"]) == 0
    interrupted = _copy_of_showcase(tmp_path, "interrupted")
    monkeypatch.setattr(sys, "stdout", _ClosedPipe())

    with pytest.raises(BrokenPipeError):
        cli.main(["convert", "-d", str(DUMP), "-r", str(interrupted), "--write"])

    assert _tree(interrupted) == _tree(read)
