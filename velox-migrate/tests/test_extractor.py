"""Tests for velox_migrate.extractor: the plugin's output, its options, and its independence."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from velox_migrate import extractor, schema

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
SUITE = CORPUS / "fixtures_showcase"
CHECKED_IN = CORPUS / "dumps" / "fixtures_showcase-pytest-9.1.json"


def run_extractor(workdir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(SUITE),
            "-p",
            "velox_migrate.extractor",
            "--collect-only",
            "-q",
            *args,
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def dump(tmp_path: Path) -> dict:
    result = run_extractor(tmp_path, "--extractor-out", "dump.json")
    assert result.returncode == 0, result.stdout + result.stderr
    return schema.load(tmp_path / "dump.json")


def test_the_plugin_writes_a_dump_the_loader_accepts(dump: dict) -> None:
    assert dump["extractor_version"] == schema.EXTRACTOR_VERSION
    assert len(dump["items"]) == 10


def test_the_output_path_is_relative_to_where_pytest_was_invoked(tmp_path: Path) -> None:
    nested = tmp_path / "somewhere"
    nested.mkdir()

    run_extractor(nested, "--extractor-out", "dump.json")

    assert (nested / "dump.json").exists()


def test_a_missing_parent_directory_is_created(tmp_path: Path) -> None:
    run_extractor(tmp_path, "--extractor-out", "nested/deeper/dump.json")

    assert (tmp_path / "nested" / "deeper" / "dump.json").exists()


def test_the_default_output_path_is_used_when_no_option_is_given(tmp_path: Path) -> None:
    run_extractor(tmp_path)

    assert (tmp_path / extractor.DEFAULT_OUT).exists()


def test_collection_alone_produces_the_dump(dump: dict) -> None:
    # `--collect-only` means no test body ran, and the dump is complete anyway. Everything the
    # tool reads is built while pytest collects.
    assert dump["items"]
    assert dump["fixture_registry"]["settings"]


def test_the_live_dump_agrees_with_the_checked_in_one(dump: dict) -> None:
    # The checked-in dumps are what every other test loads, so they have to keep matching what
    # the extractor produces. Compared on what describes the suite: the environments differ, so
    # the fixtures the environment contributes differ with them.
    expected = json.loads(CHECKED_IN.read_text(encoding="utf-8"))

    assert [item["nodeid"] for item in dump["items"]] == [
        item["nodeid"] for item in expected["items"]
    ]
    assert dump["autouse_by_node"] == expected["autouse_by_node"]
    assert _suite_fixtures(dump) == _suite_fixtures(expected)


def test_the_dump_carries_the_environment_it_was_extracted_in(dump: dict) -> None:
    # A suite whose fixtures differ by platform resolves differently per environment, so a dump
    # is only ground truth alongside a record of where it was taken.
    assert dump["environment"]["sys_platform"] == sys.platform
    assert dump["environment"]["python_version"]


def test_only_the_rootdir_is_recorded_as_a_path_on_this_machine(dump: dict) -> None:
    # Everything a later stage reads is anchored to the rootdir, which is recorded once. Leaving
    # absolute paths anywhere else would make a dump meaningless as soon as it left the machine
    # that produced it — and a dump is meant to be copied out of a container.
    described = {key: value for key, value in dump.items() if key not in ("rootpath", "args")}

    machine_paths = [
        text
        for text in _strings(described)
        if text.startswith(sys.prefix) or text.startswith(str(SUITE))
    ]

    assert machine_paths == []


def test_the_rootdir_records_where_the_extraction_happened(dump: dict) -> None:
    assert Path(dump["rootpath"]) == SUITE


def test_a_suite_path_is_recorded_relative_to_the_rootdir(dump: dict) -> None:
    settings = dump["fixture_defs"][dump["fixture_registry"]["settings"][0]]

    assert settings["func"]["file"] == "conftest.py"


def test_an_environment_path_is_recorded_against_a_prefix_token(dump: dict) -> None:
    tmp_path_fixture = dump["fixture_defs"][dump["fixture_registry"]["tmp_path"][-1]]

    assert tmp_path_fixture["func"]["file"].startswith("${")


def test_the_extractor_imports_nothing_from_the_rest_of_the_package() -> None:
    # It is copied into environments where only pytest is installed, so an import of a sibling
    # module would break exactly the case it exists for.
    source = (Path(extractor.__file__)).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "velox_migrate" not in imported


def test_the_extractor_and_the_loader_agree_on_the_dump_version() -> None:
    assert extractor.EXTRACTOR_VERSION == schema.EXTRACTOR_VERSION


def test_the_extractor_and_the_loader_agree_on_the_supported_pytest_range() -> None:
    assert extractor.MIN_PYTEST == schema.MIN_PYTEST
    assert extractor.MAX_PYTEST_EXCLUSIVE == schema.MAX_PYTEST_EXCLUSIVE


@pytest.mark.parametrize(
    "version, expected",
    [("9.1.1", (9, 1)), ("8.4", (8, 4)), ("9.2.0.dev123", (9, 2)), ("10.0.0rc1", (10, 0))],
)
def test_the_running_pytest_version_is_read_for_the_range_check(
    version: str, expected: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pytest, "__version__", version)

    assert extractor._pytest_version_tuple() == expected


def test_an_unsupported_pytest_is_refused_before_anything_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pytest, "__version__", "7.4.0")

    with pytest.raises(pytest.UsageError, match="supports pytest"):
        extractor.pytest_configure(config=None)


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/suite/tests/conftest.py", "tests/conftest.py"),
        ("/suite", "."),
        ("/elsewhere/mod.py", "/elsewhere/mod.py"),
    ],
)
def test_paths_are_rewritten_against_the_rootdir(path: str, expected: str) -> None:
    normalize = extractor._PathNormalizer("/suite")

    assert normalize(path) == expected


def test_a_path_in_the_environment_is_rewritten_against_the_prefix() -> None:
    normalize = extractor._PathNormalizer("/suite")

    rewritten = normalize(f"{sys.prefix}/lib/site-packages/plugin.py")

    assert rewritten == "${prefix}/lib/site-packages/plugin.py"


def test_a_suite_inside_the_environment_prefix_still_wins() -> None:
    # Longest match first, so a suite that lives under the interpreter prefix — a checkout inside
    # a virtualenv — is still described relative to itself.
    normalize = extractor._PathNormalizer(f"{sys.prefix}/suite")

    assert normalize(f"{sys.prefix}/suite/conftest.py") == "conftest.py"


def _suite_fixtures(dump: dict) -> dict[str, list[str]]:
    """Fixture name to the nodes it is defined at, for the fixtures the suite itself defines."""
    found: dict[str, list[str]] = {}
    for name, keys in dump["fixture_registry"].items():
        visibility = [
            dump["fixture_defs"][key]["visibility"]
            for key in keys
            if not (dump["fixture_defs"][key]["func"]["file"] or "").startswith("${")
        ]
        if visibility:
            found[name] = visibility
    return found


def _strings(value: object):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value
