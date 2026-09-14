"""`voci._affected.seeds.seeds_for_record`: the per-record driver over `World.resolve_code`
(`resolve.py`'s own tests cover that mapping itself), plus the mid-run-edit drop rule."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from voci._affected.collector import CollectorRecord
from voci._affected.resolve import DataKey, DefKey, DirKey, EnvKey, World
from voci._affected.seeds import non_code_keys_for, seeds_for_record


def _world(files: Mapping[str, str]) -> tuple[World, dict[str, Path]]:
    paths = {dotted: Path(f"/proj/{dotted.replace('.', '/')}.py") for dotted in files}
    return World({paths[dotted]: (dotted, source) for dotted, source in files.items()}), paths


def test_seeds_for_record_resolves_every_traced_code() -> None:
    world, paths = _world(
        {
            "config": "TIMEOUT = 5\n",
            "app": "from config import TIMEOUT\ndef handler():\n    return TIMEOUT\n",
        }
    )
    record = CollectorRecord(codes=frozenset({(str(paths["app"]), "handler")}))
    seeds = seeds_for_record(world, record)
    assert seeds == frozenset({DefKey(paths["app"], "handler")})


def test_seeds_for_record_unions_across_several_traced_codes() -> None:
    world, paths = _world(
        {"app": "def a():\n    return 1\ndef b():\n    return 2\n"},
    )
    record = CollectorRecord(codes=frozenset({(str(paths["app"]), "a"), (str(paths["app"]), "b")}))
    seeds = seeds_for_record(world, record)
    assert seeds == frozenset({DefKey(paths["app"], "a"), DefKey(paths["app"], "b")})


def test_seeds_for_record_is_empty_for_an_empty_record() -> None:
    world, _paths = _world({"app": "def a():\n    return 1\n"})
    assert seeds_for_record(world, CollectorRecord.empty()) == frozenset()


def test_seeds_for_record_drops_a_record_touching_a_changed_file() -> None:
    world, paths = _world({"app": "def handler():\n    return 1\n"})
    record = CollectorRecord(codes=frozenset({(str(paths["app"]), "handler")}))
    assert seeds_for_record(world, record, changed_paths=frozenset({paths["app"]})) is None


def test_seeds_for_record_keeps_a_record_whose_files_are_untouched() -> None:
    world, paths = _world(
        {"app": "def handler():\n    return 1\n", "other": "def unrelated():\n    return 2\n"}
    )
    record = CollectorRecord(codes=frozenset({(str(paths["app"]), "handler")}))
    seeds = seeds_for_record(world, record, changed_paths=frozenset({paths["other"]}))
    assert seeds == frozenset({DefKey(paths["app"], "handler")})


def test_seeds_for_record_drops_before_resolving_any_further_code() -> None:
    """One changed file anywhere in the record is enough to drop the whole thing -- a record is
    an all-or-nothing unit, not partially salvageable."""
    world, paths = _world({"app": "def a():\n    return 1\n", "other": "def b():\n    return 2\n"})
    record = CollectorRecord(
        codes=frozenset({(str(paths["app"]), "a"), (str(paths["other"]), "b")})
    )
    assert seeds_for_record(world, record, changed_paths=frozenset({paths["other"]})) is None


def test_seeds_for_record_untrusted_still_resolves_seeds() -> None:
    world, paths = _world({"app": "def handler():\n    return 1\n"})
    record = CollectorRecord(codes=frozenset({(str(paths["app"]), "handler")}), untrusted="reason")
    seeds = seeds_for_record(world, record)
    assert seeds == frozenset({DefKey(paths["app"], "handler")})


# -- non_code_keys_for --------------------------------------------------------------------------


def test_non_code_keys_for_turns_data_paths_into_data_keys() -> None:
    record = CollectorRecord(codes=frozenset(), data_paths=frozenset({"/proj/fixture.json"}))
    assert non_code_keys_for(record) == frozenset({DataKey(Path("/proj/fixture.json"))})


def test_non_code_keys_for_turns_dir_paths_into_dir_keys() -> None:
    record = CollectorRecord(codes=frozenset(), dir_paths=frozenset({"/proj/fixtures"}))
    assert non_code_keys_for(record) == frozenset({DirKey(Path("/proj/fixtures"))})


def test_non_code_keys_for_turns_env_names_into_env_keys() -> None:
    record = CollectorRecord(codes=frozenset(), env_names=frozenset({"MY_VAR"}))
    assert non_code_keys_for(record) == frozenset({EnvKey("MY_VAR")})


def test_non_code_keys_for_is_empty_for_an_empty_record() -> None:
    assert non_code_keys_for(CollectorRecord.empty()) == frozenset()


def test_non_code_keys_for_unions_all_three_kinds() -> None:
    record = CollectorRecord(
        codes=frozenset(),
        data_paths=frozenset({"/proj/fixture.json"}),
        dir_paths=frozenset({"/proj/fixtures"}),
        env_names=frozenset({"MY_VAR"}),
    )
    assert non_code_keys_for(record) == frozenset(
        {
            DataKey(Path("/proj/fixture.json")),
            DirKey(Path("/proj/fixtures")),
            EnvKey("MY_VAR"),
        }
    )
