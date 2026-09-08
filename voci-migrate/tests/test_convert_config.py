"""Tests for voci_migrate.convert.config: what a suite's pytest settings become under voci.

The corpus suites supply the settings a real ini file writes, read from the checked-in dump of each
supported pytest — the two spell `xfail_strict` differently, and a translation that reads the
suite's own file rather than pytest's resolved configuration has to be blind to that. Suites that
write the settings voci does have a key for are built on top of those dumps, since neither corpus
suite writes one.
"""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

import pytest

from voci_migrate import model
from voci_migrate.convert import config
from voci_migrate.model import GroundTruth

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
DUMPS = CORPUS / "dumps"
PYTEST_VERSIONS = ["8.4", "9.1"]

HAZARDS = "hazards_showcase"
FIXTURES = "fixtures_showcase"

BARE = "[tool.voci]\n"


def ground_truth_of(suite: str, version: str) -> GroundTruth:
    return model.load(DUMPS / f"{suite}-pytest-{version}.json")


@pytest.fixture(params=PYTEST_VERSIONS, ids=[f"pytest{v}" for v in PYTEST_VERSIONS])
def version(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def hazards(version: str) -> GroundTruth:
    return ground_truth_of(HAZARDS, version)


@pytest.fixture
def fixtures(version: str) -> GroundTruth:
    return ground_truth_of(FIXTURES, version)


def wrote(ground_truth: GroundTruth, root: Path, **resolved: str | None) -> GroundTruth:
    """`ground_truth` as a suite whose own `pytest.ini` writes exactly `resolved`'s keys.

    Each value is the `repr` pytest resolved the setting to, as a dump carries it. A key given
    `None` is one the suite wrote and the extracted pytest registered nothing for — a plugin's
    setting, extracted in an environment without the plugin.
    """
    lines = "".join(f"{key} = written\n" for key in resolved)
    (root / "pytest.ini").write_text(f"[pytest]\n{lines}", encoding="utf-8")
    ini = {key: value for key, value in ground_truth.ini.items() if key not in resolved}
    ini.update({key: value for key, value in resolved.items() if value is not None})
    return dataclasses.replace(ground_truth, ini=ini, inipath="pytest.ini")


def test_only_the_settings_voci_has_a_key_for_are_carried(hazards: GroundTruth) -> None:
    translation = config.translate(hazards, root=CORPUS / HAZARDS)

    assert translation.settings == {"filterwarnings": '["ignore::DeprecationWarning"]'}
    assert translation.carried == {"filterwarnings": "filterwarnings"}
    assert translation.conflict is None


def test_every_setting_the_suite_wrote_is_accounted_for(hazards: GroundTruth) -> None:
    translation = config.translate(hazards, root=CORPUS / HAZARDS)

    assert translation.dropped == (
        "addopts",
        "log_cli",
        "log_cli_level",
        "markers",
        "xfail_strict",
    )


def test_a_setting_is_dropped_under_the_spelling_the_suite_wrote() -> None:
    # pytest 9 renamed `xfail_strict` to `strict_xfail` and kept the old name as an alias. What the
    # suite has to answer for is the line in its file, which is the same line under both pytests.
    eight, nine = (
        config.translate(ground_truth_of(HAZARDS, version), root=CORPUS / HAZARDS)
        for version in PYTEST_VERSIONS
    )

    assert eight.dropped == nine.dropped


def test_a_suite_that_only_registers_marks_drops_that(fixtures: GroundTruth) -> None:
    translation = config.translate(fixtures, root=CORPUS / FIXTURES)

    assert translation.dropped == ("markers",)
    assert translation.table == BARE


def test_the_resolved_configuration_is_not_mistaken_for_what_the_suite_wrote(
    hazards: GroundTruth, tmp_path: Path
) -> None:
    # pytest resolves `python_files` and `norecursedirs` to its own defaults whether or not anyone
    # wrote them. With the suite's ini file out of reach there is no way to tell, so nothing is
    # carried and nothing is claimed to have been dropped.
    translation = config.translate(hazards, root=tmp_path)

    assert translation.settings == {}
    assert translation.dropped == ()
    assert translation.table == BARE


def test_the_settings_voci_has_a_key_for_are_carried(fixtures: GroundTruth, tmp_path: Path) -> None:
    ground_truth = wrote(
        fixtures,
        tmp_path,
        testpaths="['tests', 'integration']",
        python_files="['check_*.py']",
        norecursedirs="['build', '.venv']",
        timeout="'30'",
    )

    translation = config.translate(ground_truth, root=tmp_path)

    assert translation.dropped == ()
    assert translation.carried == {
        "testpaths": "testpaths",
        "timeout": "timeout",
        "python_files": "test_file_patterns",
        "norecursedirs": "ignore",
    }
    # voci's own key order, so the same suite always writes the same table.
    assert translation.table == (
        "[tool.voci]\n"
        'testpaths = ["tests", "integration"]\n'
        "timeout = 30\n"
        'test_file_patterns = ["check_*.py"]\n'
        'ignore = ["build", ".venv"]\n'
    )


def test_the_rendered_table_is_toml(fixtures: GroundTruth, tmp_path: Path) -> None:
    # Hand-rendered, since voci-migrate depends on libcst alone and `tomllib` only reads.
    ground_truth = wrote(
        fixtures,
        tmp_path,
        testpaths="['tests']",
        python_files="['check_*.py']",
        norecursedirs="['build']",
        timeout="'30'",
    )

    translation = config.translate(ground_truth, root=tmp_path)

    assert tomllib.loads(translation.table)["tool"]["voci"] == {
        "testpaths": ["tests"],
        "timeout": 30,
        "test_file_patterns": ["check_*.py"],
        "ignore": ["build"],
    }


def test_a_timeout_is_written_as_the_number_it_is(fixtures: GroundTruth, tmp_path: Path) -> None:
    for resolved, expected in (("'30'", "30"), ("'0.5'", "0.5"), ("12.5", "12.5"), ("7", "7")):
        translation = config.translate(wrote(fixtures, tmp_path, timeout=resolved), root=tmp_path)

        assert translation.settings == {"timeout": expected}


def test_a_value_that_cannot_be_rendered_is_dropped_rather_than_guessed_at(
    fixtures: GroundTruth, tmp_path: Path
) -> None:
    ground_truth = wrote(
        fixtures,
        tmp_path,
        testpaths="<Path object at 0x1>",  # no literal describes what pytest resolved
        python_files="[]",  # written with no value; an empty voci list would collect nothing
        norecursedirs="'build'",  # a bare string where voci takes a list
        timeout="'soon'",
    )

    translation = config.translate(ground_truth, root=tmp_path)

    assert translation.settings == {}
    assert translation.carried == {}
    assert translation.dropped == ("norecursedirs", "python_files", "testpaths", "timeout")


def test_a_setting_the_extracted_pytest_never_registered_is_dropped(
    fixtures: GroundTruth, tmp_path: Path
) -> None:
    ground_truth = wrote(fixtures, tmp_path, timeout=None)

    translation = config.translate(ground_truth, root=tmp_path)

    assert translation.settings == {}
    assert translation.dropped == ("timeout",)


def test_a_quote_or_a_backslash_in_a_value_is_escaped(
    fixtures: GroundTruth, tmp_path: Path
) -> None:
    ground_truth = wrote(fixtures, tmp_path, norecursedirs=repr(['say "no"', "build\\out"]))

    translation = config.translate(ground_truth, root=tmp_path)

    assert translation.settings == {"ignore": '["say \\"no\\"", "build\\\\out"]'}
    assert tomllib.loads(translation.table)["tool"]["voci"]["ignore"] == [
        'say "no"',
        "build\\out",
    ]


def test_a_value_no_toml_string_can_hold_is_dropped(fixtures: GroundTruth, tmp_path: Path) -> None:
    ground_truth = wrote(fixtures, tmp_path, norecursedirs=repr(["build\x00out"]))

    translation = config.translate(ground_truth, root=tmp_path)

    assert translation.settings == {}
    assert "norecursedirs" in translation.dropped


def test_a_suite_with_no_pyproject_toml_gets_one(hazards: GroundTruth, tmp_path: Path) -> None:
    translation = config.translate(hazards, root=tmp_path)

    written = config.edit(translation, root=tmp_path)

    assert written is not None
    assert written.path == "pyproject.toml"
    assert written.kind == "create"
    assert written.new_text == BARE


def test_the_table_is_appended_after_a_blank_line(hazards: GroundTruth, tmp_path: Path) -> None:
    existing = '[project]\nname = "suite"\n'
    (tmp_path / "pyproject.toml").write_text(existing, encoding="utf-8")
    translation = config.translate(hazards, root=tmp_path)

    written = config.edit(translation, root=tmp_path)

    assert written is not None
    assert written.kind == "modify"
    assert written.old_text == existing
    assert written.new_text == f"{existing}\n{BARE}"


def test_a_file_that_already_ends_in_a_blank_line_gains_no_second_one(
    hazards: GroundTruth, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "suite"\n\n', encoding="utf-8")
    translation = config.translate(hazards, root=tmp_path)

    written = config.edit(translation, root=tmp_path)

    assert written is not None
    assert written.new_text == f'[project]\nname = "suite"\n\n{BARE}'


def test_a_file_that_ends_without_a_newline_still_gets_a_blank_line(
    hazards: GroundTruth, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "suite"', encoding="utf-8")
    translation = config.translate(hazards, root=tmp_path)

    written = config.edit(translation, root=tmp_path)

    assert written is not None
    assert written.new_text == f'[project]\nname = "suite"\n\n{BARE}'


def test_an_empty_pyproject_toml_gets_the_table_alone(hazards: GroundTruth, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("\n", encoding="utf-8")
    translation = config.translate(hazards, root=tmp_path)

    written = config.edit(translation, root=tmp_path)

    assert written is not None
    assert written.new_text == BARE


def test_the_table_this_conversion_already_wrote_is_left_alone(
    fixtures: GroundTruth, tmp_path: Path
) -> None:
    # Converting a converted suite again: the table is recognized as the one it would write, so
    # there is no edit and nothing to explain.
    ground_truth = wrote(fixtures, tmp_path, testpaths="['tests']")
    first = config.translate(ground_truth, root=tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "suite"\n\n{first.table}', encoding="utf-8"
    )

    again = config.translate(ground_truth, root=tmp_path)

    assert again.conflict is None
    assert config.edit(again, root=tmp_path) is None


def test_a_table_followed_by_another_is_read_as_far_as_its_own_keys(
    fixtures: GroundTruth, tmp_path: Path
) -> None:
    ground_truth = wrote(fixtures, tmp_path, testpaths="['tests']")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.voci]\ntestpaths = ["tests"]\n\n[tool.ruff]\nline-length = 100\n', encoding="utf-8"
    )

    translation = config.translate(ground_truth, root=tmp_path)

    assert translation.conflict is None
    assert config.edit(translation, root=tmp_path) is None


def test_a_table_that_says_something_else_is_kept_and_explained(
    fixtures: GroundTruth, tmp_path: Path
) -> None:
    ground_truth = wrote(fixtures, tmp_path, testpaths="['tests']")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.voci]\nconcurrency = 4\ntimeout = 60\n", encoding="utf-8"
    )

    translation = config.translate(ground_truth, root=tmp_path)

    assert translation.conflict is not None
    assert "concurrency, timeout" in translation.conflict
    assert "testpaths" in translation.conflict
    assert config.edit(translation, root=tmp_path) is None
    # The translation itself still says what the suite's settings were worth.
    assert translation.settings == {"testpaths": '["tests"]'}


def test_a_sub_table_counts_as_a_configured_voci(hazards: GroundTruth, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.voci.env]\nTZ = "UTC"\n', encoding="utf-8")

    translation = config.translate(hazards, root=tmp_path)

    assert translation.conflict is not None
    assert "env" in translation.conflict
    assert config.edit(translation, root=tmp_path) is None


def test_only_keys_voci_recognizes_are_ever_written(fixtures: GroundTruth, tmp_path: Path) -> None:
    # An unknown `[tool.voci]` key is a hard error at run time, so the carried set is closed.
    known = {
        "testpaths",
        "concurrency",
        "timeout",
        "loop_watchdog",
        "test_file_patterns",
        "ignore",
        "filterwarnings",
    }

    assert set(config.CARRIED.values()) <= known
