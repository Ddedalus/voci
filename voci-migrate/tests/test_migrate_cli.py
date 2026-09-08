"""Tests for voci_migrate.cli: argument handling and the exit codes `extract` reports."""

from __future__ import annotations

import errno
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _support import git_commit, git_repo

from voci_migrate import cli, extractor, schema, workspace
from voci_migrate.convert.edits import EditSet

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
        "import voci_migrate.cli, voci_migrate.model, voci_migrate.schema\n"
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
    assert "voci-migrate extract" in capsys.readouterr().err


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


def test_converting_with_write_records_a_pytest_baseline_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The suite is real and green under plain pytest, so --write has something honest to record
    # before it overwrites it.
    suite = _copy_of_showcase(tmp_path, "suite")
    before_conftest = (suite / "conftest.py").read_text(encoding="utf-8")

    code = cli.main(["convert", "-d", str(DUMP), "-r", str(suite), "--write"])

    assert code == 0
    out = capsys.readouterr().out
    assert "recorded" in out and "pytest outcome" in out
    baseline = suite / workspace.BASELINE_DIR
    outcomes = json.loads((baseline / workspace.OUTCOMES_NAME).read_text(encoding="utf-8"))
    assert outcomes["exit_status"] == 0
    # The snapshot is what pytest saw: the suite as it stood before conversion, not after.
    assert (baseline / workspace.TREE_DIR / "conftest.py").read_text(
        encoding="utf-8"
    ) == before_conftest


def test_converting_with_write_records_a_baseline_under_a_suites_own_norecursedirs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A suite that sets its own `norecursedirs` (marshmallow does) replaces pytest's default
    # rather than extending it, dropping the `.*` wildcard that would otherwise skip back over
    # `.voci-migrate/baseline/tree` -- the snapshot `_record_baseline` just copied the whole suite
    # into. Left alone, pytest walks into that copy too and collects `test_top.py` from both
    # paths, raising `ImportPathMismatchError` and aborting the baseline recording.
    suite = _copy_of_showcase(tmp_path, "suite")
    ini = suite / "pytest.ini"
    ini.write_text(
        ini.read_text(encoding="utf-8") + "norecursedirs = .tox venv\n", encoding="utf-8"
    )

    code = cli.main(["convert", "-d", str(DUMP), "-r", str(suite), "--write"])

    assert code == 0
    out = capsys.readouterr().out
    assert "recorded" in out and "pytest outcome" in out
    baseline = suite / workspace.BASELINE_DIR
    outcomes = json.loads((baseline / workspace.OUTCOMES_NAME).read_text(encoding="utf-8"))
    assert outcomes["exit_status"] == 0


def test_converting_with_write_and_nothing_to_change_skips_the_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    suite = _copy_of_showcase(tmp_path, "suite")
    monkeypatch.setattr(EditSet, "diff", lambda self: "")

    code = cli.main(["convert", "-d", str(DUMP), "-r", str(suite), "--write"])

    assert code == 0
    assert "recorded" not in capsys.readouterr().out
    assert not (suite / ".voci-migrate").exists()


def test_converting_with_write_and_no_pytest_here_refuses_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    suite = _copy_of_showcase(tmp_path, "suite")
    before = _tree(suite)
    real_find_spec = cli.importlib.util.find_spec
    monkeypatch.setattr(
        cli.importlib.util,
        "find_spec",
        lambda name, *a, **kw: None if name == "pytest" else real_find_spec(name, *a, **kw),
    )

    code = cli.main(["convert", "-d", str(DUMP), "-r", str(suite), "--write"])

    assert code == 1
    assert "no pytest" in capsys.readouterr().err
    assert _tree(suite) == before
    assert not (suite / ".voci-migrate").exists()


def test_a_reader_who_quits_part_way_through_the_diff_still_gets_the_whole_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reading the plan through a pager and quitting it fails this process's next write. The tree
    # left behind has to be the converted one, not however much of it had been printed before the
    # pipe closed, and the run is over rather than failed.
    read = _copy_of_showcase(tmp_path, "read")
    assert cli.main(["convert", "-d", str(DUMP), "-r", str(read), "--write"]) == 0
    interrupted = _copy_of_showcase(tmp_path, "interrupted")
    monkeypatch.setattr(sys, "stdout", _ClosedPipe())

    code = cli.main(["convert", "-d", str(DUMP), "-r", str(interrupted), "--write"])

    assert code == 0
    assert _tree(interrupted) == _tree(read)


def test_a_write_that_fails_part_way_names_the_tree_it_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    suite = _copy_of_showcase(tmp_path, "suite")

    def refuse(self: EditSet, root: Path) -> tuple[str, ...]:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(EditSet, "apply", refuse)

    code = cli.main(["convert", "-d", str(DUMP), "-r", str(suite), "--write"])

    assert code == 1
    assert str(suite) in capsys.readouterr().err


def test_scaffold_cli_rebases_and_exports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = git_repo(tmp_path / "source")
    (source / "test_thing.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    tip = git_commit(source, "initial")
    dest = tmp_path / "dest"

    code = cli.main(["scaffold", str(source), str(dest)])

    assert code == 0
    out = capsys.readouterr().out
    assert tip[:12] in out
    assert "relocation fixups go in" in out
    assert (dest / "test_thing.py").is_file()


def test_scaffold_cli_reports_a_conflict_with_a_nonzero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = git_repo(tmp_path / "source")
    (source / "conftest.py").write_text("ROOT = 'orig'\n", encoding="utf-8")
    git_commit(source, "initial")
    dest = tmp_path / "dest"
    assert cli.main(["scaffold", str(source), str(dest)]) == 0

    scratch = workspace._scratch_worktree(source.resolve(), workspace.RELOCATION_BRANCH)
    (scratch / "conftest.py").write_text("ROOT = 'fixup'\n", encoding="utf-8")
    git_commit(scratch, "relocation fixup")
    (source / "conftest.py").write_text("ROOT = 'prefactor'\n", encoding="utf-8")
    git_commit(source, "prefactor touches the same line")

    code = cli.main(["scaffold", str(source), str(dest)])

    assert code == 1
    assert "does not rebase cleanly" in capsys.readouterr().err
