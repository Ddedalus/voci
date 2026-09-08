"""Tests for voci_migrate.schema: dump loading, version gating, and shape validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voci_migrate import schema

CORPUS_DUMP = (
    Path(__file__).parent.parent / "corpus" / "dumps" / "fixtures_showcase-pytest-9.1.json"
)


def _valid_dump() -> dict:
    """A mutable copy of the checked-in corpus dump, the starting point for building bad input."""
    return json.loads(CORPUS_DUMP.read_text(encoding="utf-8"))


def test_load_returns_every_top_level_key() -> None:
    dump = schema.load(CORPUS_DUMP)

    assert set(dump) == set(schema._TOP_LEVEL)


def test_loads_accepts_the_same_content_as_text() -> None:
    text = CORPUS_DUMP.read_text(encoding="utf-8")

    assert schema.loads(text) == schema.load(CORPUS_DUMP)


def test_load_of_a_missing_dump_names_the_extractor(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-dump.json"

    with pytest.raises(schema.DumpError, match="voci-migrate extract"):
        schema.load(missing)


def test_loads_malformed_json_raises() -> None:
    with pytest.raises(schema.DumpError, match="not valid JSON"):
        schema.loads("{not valid json")


def test_loads_a_json_list_is_refused() -> None:
    with pytest.raises(schema.DumpError, match="not a ground-truth dump"):
        schema.loads("[1, 2, 3]")


def test_dump_with_no_extractor_version_is_not_recognized() -> None:
    dump = _valid_dump()
    del dump["extractor_version"]

    with pytest.raises(schema.DumpError, match="did not come from"):
        schema.loads(json.dumps(dump))


def test_dump_from_a_newer_extractor_is_refused() -> None:
    dump = _valid_dump()
    dump["extractor_version"] = schema.EXTRACTOR_VERSION + 1

    with pytest.raises(schema.DumpError, match="newer"):
        schema.loads(json.dumps(dump))


def test_dump_from_an_older_extractor_is_refused() -> None:
    dump = _valid_dump()
    dump["extractor_version"] = schema.EXTRACTOR_VERSION - 1

    with pytest.raises(schema.DumpError, match="older"):
        schema.loads(json.dumps(dump))


@pytest.mark.parametrize("key", sorted(schema._TOP_LEVEL))
def test_dump_missing_a_top_level_key_names_it(key: str) -> None:
    dump = _valid_dump()
    del dump[key]

    # Every top-level key, including `extractor_version`, must be named in the error -- the
    # parametrize source is `schema._TOP_LEVEL` itself so a new required key can't go untested.
    with pytest.raises(schema.DumpError, match=key):
        schema.loads(json.dumps(dump))


@pytest.mark.parametrize(
    ("key", "wrong_value"),
    [("environment", ["not", "a", "dict"]), ("inipath", 42)],
)
def test_top_level_key_of_the_wrong_type_names_key_and_expectation(
    key: str, wrong_value: object
) -> None:
    dump = _valid_dump()
    dump[key] = wrong_value

    with pytest.raises(schema.DumpError, match=rf"`{key}`.*expected"):
        schema.loads(json.dumps(dump))


@pytest.mark.parametrize("version", ["7.4.0", "10.0.0"])
def test_pytest_version_outside_the_supported_range_is_refused(version: str) -> None:
    dump = _valid_dump()
    dump["pytest_version"] = version

    with pytest.raises(schema.DumpError, match="outside the supported range"):
        schema.loads(json.dumps(dump))


def test_pytest_version_inside_the_supported_range_is_accepted() -> None:
    dump = _valid_dump()
    dump["pytest_version"] = "9.2.0.dev123"

    result = schema.loads(json.dumps(dump))

    assert result["pytest_version"] == "9.2.0.dev123"


def test_fixture_def_missing_a_key_names_the_fixture() -> None:
    dump = _valid_dump()
    fixture_id = next(iter(dump["fixture_defs"]))
    del dump["fixture_defs"][fixture_id]["scope"]

    with pytest.raises(schema.DumpError, match=fixture_id):
        schema.loads(json.dumps(dump))


def test_item_missing_nodeid_raises() -> None:
    dump = _valid_dump()
    del dump["items"][0]["nodeid"]

    with pytest.raises(schema.DumpError, match="missing `nodeid`"):
        schema.loads(json.dumps(dump))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9.1.1", (9, 1, 1)),
        # A dev version compares as the release it is heading for: the suffix is dropped
        # entirely rather than parsed as a fourth release field.
        ("9.2.0.dev123", (9, 2, 0)),
        ("8.4", (8, 4)),
        ("garbage", None),
    ],
)
def test_parse_version_reads_leading_release_fields(
    raw: str, expected: tuple[int, ...] | None
) -> None:
    assert schema.parse_version(raw) == expected
