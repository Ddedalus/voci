"""Tests for velox_migrate.model: fixture chains, per-test resolution, and the version shims.

Every test here runs against the checked-in dump from each supported pytest, because the point
of the extractor's shims is that both versions arrive as one model.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from velox_migrate import model, schema
from velox_migrate.schema import DumpError

DUMPS = Path(__file__).resolve().parents[1] / "corpus" / "dumps"
PYTEST_VERSIONS = ["8.4", "9.1"]

INTEGRATION_ENGINE = "integration/test_integration.py::test_engine"
# The nodeids of the two directories that hold a conftest, which is how both a fixture's
# visibility and an autouse fixture's placement are keyed.
SUITE_ROOT = "."
INTEGRATION = "integration"


@pytest.fixture(params=PYTEST_VERSIONS, ids=[f"pytest{v}" for v in PYTEST_VERSIONS])
def ground_truth(request: pytest.FixtureRequest) -> model.GroundTruth:
    return model.load(DUMPS / f"fixtures_showcase-pytest-{request.param}.json")


def test_every_corpus_dump_loads(ground_truth: model.GroundTruth) -> None:
    assert len(ground_truth.items) == 10
    assert ground_truth.fixture_registry["settings"]
    assert ground_truth.environment["sys_platform"]


def test_the_corpus_covers_both_ends_of_the_supported_pytest_range() -> None:
    # Guards the premise of every other test in this file: if both dumps came from the same
    # pytest, the parametrization proves nothing about the extractor's shims.
    stamped = {
        (
            schema.parse_version(
                model.load(DUMPS / f"fixtures_showcase-pytest-{v}.json").pytest_version
            )
            or ()
        )[:2]
        for v in PYTEST_VERSIONS
    }

    assert stamped == {(8, 4), (9, 1)}


def test_an_override_chain_runs_furthest_to_closest(ground_truth: model.GroundTruth) -> None:
    item = ground_truth.item(INTEGRATION_ENGINE)

    chain = item.chains["settings"]

    assert [fixture.visibility for fixture in chain] == [SUITE_ROOT, INTEGRATION]


def test_the_closest_definition_is_the_one_a_test_gets(ground_truth: model.GroundTruth) -> None:
    item = ground_truth.item(INTEGRATION_ENGINE)

    assert _resolve(item, "settings").visibility == INTEGRATION


def test_an_override_requesting_its_own_name_reaches_the_fixture_it_overrides(
    ground_truth: model.GroundTruth,
) -> None:
    item = ground_truth.item(INTEGRATION_ENGINE)
    override = _resolve(item, "settings")

    dependencies = item.dependencies(override)

    assert [(edge.name, _visibility(edge)) for edge in dependencies] == [("settings", SUITE_ROOT)]


def test_a_fixture_reaches_the_override_visible_to_the_test_requesting_it(
    ground_truth: model.GroundTruth,
) -> None:
    # `engine` is defined once, at the suite root, but the test that requests it lives under
    # `integration/` — so its `settings` edge lands on the override, not on the definition
    # sitting next to it. This is the whole reason the graph hangs off the item.
    item = ground_truth.item(INTEGRATION_ENGINE)

    dependencies = item.dependencies(_resolve(item, "engine"))

    assert [(edge.name, _visibility(edge)) for edge in dependencies] == [("settings", INTEGRATION)]


def test_a_root_fixture_reaches_the_root_definition_for_a_root_test(
    ground_truth: model.GroundTruth,
) -> None:
    item = ground_truth.item("test_top.py::TestGroup::test_method")

    assert _resolve(item, "settings").visibility == SUITE_ROOT


def test_walking_a_test_reaches_both_ends_of_an_override_chain(
    ground_truth: model.GroundTruth,
) -> None:
    item = ground_truth.item(INTEGRATION_ENGINE)

    reached = [(fixture.argname, fixture.visibility) for fixture in item.walk()]

    assert ("settings", INTEGRATION) in reached
    assert ("settings", SUITE_ROOT) in reached
    assert ("engine", SUITE_ROOT) in reached


def test_walking_yields_each_definition_once(ground_truth: model.GroundTruth) -> None:
    item = ground_truth.item(INTEGRATION_ENGINE)

    keys = [fixture.key for fixture in item.walk()]

    assert len(keys) == len(set(keys))


def test_request_is_an_edge_with_no_definition(ground_truth: model.GroundTruth) -> None:
    item = ground_truth.item("test_top.py::test_indirect[mysql]")

    edges = item.dependencies(_resolve(item, "backend"))

    assert [(edge.name, edge.fixture) for edge in edges] == [("request", None)]


def test_autouse_placement_is_keyed_by_the_directory_it_applies_to(
    ground_truth: model.GroundTruth,
) -> None:
    assert ground_truth.autouse_by_node[SUITE_ROOT] == ("root_autouse",)
    assert ground_truth.autouse_by_node[INTEGRATION] == ("integ_autouse",)


def test_a_nested_test_collects_autouse_from_every_level_above_it(
    ground_truth: model.GroundTruth,
) -> None:
    item = ground_truth.item(INTEGRATION_ENGINE)

    assert item.autouse_names == ("root_autouse", "integ_autouse")


def test_usefixtures_is_not_counted_as_autouse(ground_truth: model.GroundTruth) -> None:
    item = ground_truth.item("test_top.py::test_uses")

    assert item.usefixtures == ("wrapped_fix",)
    assert item.autouse_names == ("root_autouse",)


def test_a_directly_parametrized_argument_is_marked_as_one(
    ground_truth: model.GroundTruth,
) -> None:
    # pytest 9.1 backs a direct argument with a distinct class and 8.4 with a shared function;
    # either way the dump has to answer "is this a fixture or a parameter" the same.
    item = ground_truth.item("test_top.py::test_direct[two]")

    assert _resolve(item, "n").direct_param is True


def test_an_indirect_argument_resolves_to_the_real_fixture(
    ground_truth: model.GroundTruth,
) -> None:
    item = ground_truth.item("test_top.py::test_indirect[mysql]")

    backend = _resolve(item, "backend")

    assert backend.direct_param is False
    assert backend.is_parametrized


def test_pytest_generated_ids_are_carried_verbatim(ground_truth: model.GroundTruth) -> None:
    # An explicit `id=`, an int and a float, each rendered by pytest's own id machinery. They are
    # copied out rather than regenerated, so they survive changes to how pytest builds them.
    ids = [_callspec(item).id for item in ground_truth.items if item.originalname == "test_direct"]

    assert ids == ["1", "two", "3.5"]


def test_a_params_fixture_parametrizes_a_test_that_never_asked_for_it(
    ground_truth: model.GroundTruth,
) -> None:
    # `test_params_fixture` carries no parametrize mark; the ids come from the fixture's own
    # `ids=`, and the index is the position in that fixture's `params`.
    item = ground_truth.item("test_top.py::test_params_fixture[pg]")

    assert item.is_parametrized
    assert _callspec(item).id == "pg"
    assert _callspec(item).indices["backend"] == 1


def test_a_per_case_mark_is_distinguishable_from_a_decorator_mark(
    ground_truth: model.GroundTruth,
) -> None:
    item = ground_truth.item("test_top.py::test_direct[two]")

    assert [mark.name for mark in _callspec(item).marks] == ["xfail"]


def test_marks_carry_the_node_they_were_written_on(ground_truth: model.GroundTruth) -> None:
    item = ground_truth.item("test_top.py::TestGroup::test_method")

    origins = {mark.name: mark.origin for mark in item.markers_with_origin}

    assert origins["classmark"] == "test_top.py::TestGroup"
    assert origins["modmark"] == "test_top.py"


def test_a_wrapped_fixture_points_at_the_function_under_the_decorator(
    ground_truth: model.GroundTruth,
) -> None:
    wrapped = ground_truth.fixture_registry["wrapped_fix"][-1]

    assert wrapped.func.wrapped is True
    assert wrapped.func.file == "conftest.py"
    assert wrapped.func.qualname == "wrapped_fix"


def test_a_dynamically_requested_fixture_is_absent_from_the_closure(
    ground_truth: model.GroundTruth,
) -> None:
    # `dyn` calls `request.getfixturevalue("hidden")`, which collection cannot see. Nothing may
    # claim otherwise, because a static pass over the sources is the only thing that finds it.
    item = ground_truth.item("test_top.py::test_uses")

    assert "hidden" not in item.names_closure


def test_an_ini_key_renamed_by_pytest_is_found_under_the_name_the_suite_uses(
    ground_truth: model.GroundTruth,
) -> None:
    # pytest 9.1 renamed `xfail_strict` to `strict_xfail` and kept the old name as an alias, so
    # the two dumps file the same setting under different keys. A suite's config still spells it
    # the old way, and that spelling has to find it in both.
    assert ground_truth.ini_value("xfail_strict") is not None


def test_fixture_paths_are_relative_to_the_suite(ground_truth: model.GroundTruth) -> None:
    settings = ground_truth.fixture_registry["settings"][0]

    assert settings.func.file == "conftest.py"


def test_a_plugin_fixture_keeps_a_prefix_token_instead_of_a_machine_path(
    ground_truth: model.GroundTruth,
) -> None:
    tmp_path = ground_truth.fixture_registry["tmp_path"][-1]

    assert (tmp_path.func.file or "").startswith("${")


def test_resolving_a_suite_path_joins_it_to_the_rootdir(
    ground_truth: model.GroundTruth, tmp_path: Path
) -> None:
    # The checked-in dumps carry a relative rootdir, so the suite has to be pointed at wherever
    # it actually sits before its paths mean anything.
    relocated = dataclasses.replace(ground_truth, rootpath=str(tmp_path))

    assert relocated.resolve_path("conftest.py") == tmp_path / "conftest.py"


def test_resolving_an_environment_path_needs_the_prefix_it_was_extracted_under(
    ground_truth: model.GroundTruth,
) -> None:
    assert ground_truth.resolve_path("${prefix}/lib/mod.py") is None
    assert ground_truth.resolve_path("${prefix}/lib/mod.py", prefix="/env") == Path(
        "/env/lib/mod.py"
    )


def test_a_chain_naming_a_definition_the_dump_lacks_is_refused() -> None:
    dump = _raw("9.1")
    dump["fixture_registry"]["settings"] = ["nonexistent"]

    with pytest.raises(DumpError, match="does not define"):
        model.build(dump)


def test_an_item_chain_naming_a_missing_definition_is_refused() -> None:
    dump = _raw("9.1")
    dump["items"][0]["name2fixturedefs"] = {"settings": ["nonexistent"]}

    with pytest.raises(DumpError, match="does not define"):
        model.build(dump)


def _visibility(edge: model.Dependency) -> str:
    assert edge.fixture is not None, f"{edge.name} did not resolve to a definition"
    return edge.fixture.visibility


def _resolve(item: model.Item, name: str) -> model.FixtureDef:
    fixture = item.resolve(name)
    assert fixture is not None, f"{item.nodeid} does not resolve {name!r}"
    return fixture


def _callspec(item: model.Item) -> model.CallSpec:
    assert item.callspec is not None, f"{item.nodeid} is not parametrized"
    return item.callspec


def _raw(version: str) -> dict:
    return schema.load(DUMPS / f"fixtures_showcase-pytest-{version}.json")
