"""Tests for velox_migrate.convert.specialize: one overridden name becoming two objects.

The corpus suite is the subject. `settings` is written at the root and redefined under
`integration/`, with `engine` and `client` written at the root and reaching it from there; `token`
is redefined under `leaf/` with nothing between it and the tests. What the conversion has to get
right is which of the two objects each consumer names, and that is asked of the converted source
rather than of the plan, since the source is what runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _support import DUMPS, conversion_of, converted

from velox_migrate import model
from velox_migrate.audit import wiring
from velox_migrate.convert import plan, specialize
from velox_migrate.convert.layout import Import

OVERRIDES = "overrides_showcase"
HAZARDS = "hazards_showcase"


@pytest.fixture
def tree(version: str, tmp_path: Path) -> Path:
    """The overrides corpus suite, copied out and converted in place."""
    converted(OVERRIDES, version, tmp_path / OVERRIDES)
    return tmp_path / OVERRIDES


def source(tree: Path, path: str) -> str:
    return (tree / path).read_text(encoding="utf-8")


def test_the_chain_above_an_override_is_copied_beside_it(tree: Path) -> None:
    body = source(tree, "integration/fixtures.py")

    assert "def engine_integration(settings: Annotated[Any, Depends(settings)]):" in body
    assert "def client_integration(engine: Annotated[Any, Depends(engine_integration)]):" in body


def test_the_copy_is_named_for_the_directory_that_overrides(tree: Path, version: str) -> None:
    # Two directories may each override the same name, and the copies of one chain then land
    # beside the copies of the other under names that have to stay apart.
    copies = conversion_of(OVERRIDES, version).plan.specialized.copies

    assert {copy.symbol for copy in copies.values()} == {"engine_integration", "client_integration"}
    assert {copy.node for copy in copies.values()} == {"integration"}


def test_the_original_chain_keeps_the_definition_it_was_written_above(tree: Path) -> None:
    # The tests outside the overriding directory still resolve the root `settings`, so the
    # originals are untouched — copying is what leaves them that way.
    body = source(tree, "fixtures.py")

    assert "def engine(settings: Annotated[Any, Depends(settings)]):" in body
    assert "integration" not in body[body.index("import json") :]


def test_a_test_under_the_override_names_the_copy(tree: Path) -> None:
    body = source(tree, "integration/test_integration.py")

    assert "def test_engine(engine: Annotated[Any, Depends(engine_integration)]):" in body
    assert "def test_client(client: Annotated[Any, Depends(client_integration)]):" in body


def test_a_test_outside_the_override_names_the_original(tree: Path) -> None:
    body = source(tree, "test_root.py")

    assert "def test_engine(engine: Annotated[Any, Depends(engine)]):" in body
    assert "from fixtures import client, engine, token" in body


def test_a_test_below_the_overriding_directory_reads_the_same_copy(tree: Path) -> None:
    # velox reads no directory at run time, so a test two levels down has to be pointed at the
    # copy by its import like any other consumer.
    body = source(tree, "integration/deep/test_deep.py")

    assert "from integration.fixtures import engine_integration" in body
    assert "def test_deep_engine(engine: Annotated[Any, Depends(engine_integration)]):" in body


def test_a_fixture_beside_the_override_names_the_copy(tree: Path) -> None:
    body = source(tree, "integration/fixtures.py")

    assert "def report(client: Annotated[Any, Depends(client_integration)]):" in body


def test_a_copy_is_written_below_everything_it_names(tree: Path) -> None:
    # A `Depends()` is read when the `def` under it is, so the order the copies are written in is
    # part of whether the module imports at all.
    body = source(tree, "integration/fixtures.py")

    assert body.index("def settings(") < body.index("def engine_integration(")
    assert body.index("def engine_integration(") < body.index("def client_integration(")
    assert body.index("def client_integration(") < body.index("def report(")


def test_a_copy_carries_the_names_its_body_reads(tree: Path) -> None:
    # `client`'s body reads a helper its module imported and a constant its module defines, and
    # the copy is written in a module that had neither.
    body = source(tree, "integration/fixtures.py")

    assert "import json" in body
    assert "from support import stamp" in body
    assert "from fixtures import LABEL" in body


def test_an_override_with_nothing_downstream_copies_nothing(tree: Path) -> None:
    body = source(tree, "leaf/fixtures.py")

    assert "def token():" in body
    assert "token_leaf" not in body
    assert "from leaf.fixtures import token" in source(tree, "leaf/test_leaf.py")


def test_an_override_requesting_its_own_name_names_the_definition_it_overrides(tree: Path) -> None:
    body = source(tree, "integration/fixtures.py")

    assert "from fixtures import LABEL, settings as root_settings" in body
    assert "def settings(settings: Annotated[Any, Depends(root_settings)]):" in body


def test_a_budget_the_chain_no_longer_fits_refuses_it_whole(version: str) -> None:
    # The fan-out is three, so a budget of two is the same suite over the line: nothing is copied,
    # and every test that read the chain keeps its pytest source rather than reading the wrong one.
    result = conversion_of(OVERRIDES, version, budget=2)
    ground_truth = model.load(DUMPS / f"{OVERRIDES}-pytest-{version}.json")
    blocked = {ground_truth.fixture_defs[key].argname for key in result.plan.blocked_fixtures}

    assert result.plan.specialized.copies == {}
    assert blocked == {"settings", "engine", "client", "report"}
    assert result.plan.blocked_tests >= {
        "integration/test_integration.py::test_engine",
        "integration/deep/test_deep.py::test_deep_engine",
    }
    assert "leaf/test_leaf.py::test_token" not in result.plan.blocked_tests


def test_the_refusal_quotes_the_fan_out_the_audit_computed(version: str) -> None:
    result = conversion_of(OVERRIDES, version, budget=2)
    (refusal,) = [finding for finding in result.plan.refusals if finding.code == "VX006"]

    assert refusal.detail["fan_out"] == 3
    assert refusal.detail["budget"] == 2
    assert refusal.detail["duplicated"] == "client, engine"


def test_an_override_of_an_autouse_fixture_leaves_both_definitions_alone(version: str) -> None:
    # A `velox.use(...)` names one object for a directory, so an autouse fixture an override
    # changes would be declared once for each definition over tests that had exactly one of them.
    result = conversion_of(HAZARDS, version)
    ground_truth = model.load(DUMPS / f"{HAZARDS}-pytest-{version}.json")
    blocked = {
        ground_truth.fixture_defs[key].argname
        for key in result.plan.blocked_fixtures
        if key in ground_truth.fixture_defs
    }

    assert "patched_env" in blocked
    assert not any("patched_env" in declaration.keys for declaration in result.plan.declarations)


def test_a_fixture_written_inside_the_overriding_directory_is_not_copied(version: str) -> None:
    # It is only ever resolved by tests under that directory, which already reach the override, so
    # the copy would be a second object nothing needs.
    ground_truth = model.load(DUMPS / f"{OVERRIDES}-pytest-{version}.json")
    (override,) = [
        found for found in wiring.overrides(ground_truth) if found.winner.argname == "settings"
    ]
    downstream = {ground_truth.fixture_defs[key].argname for key in override.downstream}

    assert downstream == {"engine", "client"}
    assert "report" not in downstream


def test_a_declaration_inside_the_subtree_names_the_copy(tree: Path) -> None:
    # A `usefixtures` mark hands a module a fixture it never names, and inside an overriding
    # directory the object those tests get is that directory's copy.
    body = source(tree, "integration/test_declared.py")

    assert "velox.use(client_integration)" in body
    assert "from integration.fixtures import client_integration" in body


def test_specialization_is_the_row_the_matrix_says_it_is() -> None:
    assert "VX005" not in plan.DEFERRED


# --- resolving a name from a node ---------------------------------------------------------------


def fixture_def(argname: str, visibility: str) -> model.FixtureDef:
    return model.FixtureDef(
        key=f"{visibility}:{argname}",
        argname=argname,
        scope="function",
        params=None,
        ids=None,
        autouse=False,
        visibility=visibility,
        kind="fixture",
        direct_param=False,
        argnames=(),
        returns=None,
        func=model.FuncLocation(None, None, None, None, False),
    )


def test_the_rootdir_node_holds_every_node_under_it() -> None:
    # The rootdir is spelled `"."` and a globally registered fixture's node is empty; a fixture
    # visible at either is visible to the whole suite.
    assert wiring.under(".", "integration/test_it.py::test_x")
    assert wiring.under("", "conftest.py")
    assert wiring.under("integration", "integration/deep/test_it.py")
    assert not wiring.under("integration", "leaf/test_it.py")


def test_a_consumer_takes_the_nearest_definition_above_it() -> None:
    root, override = fixture_def("settings", "."), fixture_def("settings", "integration")

    assert plan._visible_at([root, override], ".") is root
    assert plan._visible_at([root, override], "integration") is override


def test_a_name_defined_only_below_the_consumer_is_the_one_it_gets() -> None:
    # Only that subtree's tests can ask for a fixture written above them and wired to a name only
    # they can see, and the definition they resolve is the one there is.
    override = fixture_def("settings", "integration")

    assert plan._visible_at([override], ".") is override
    assert plan._visible_at([], ".") is None


# --- what a copy carries ------------------------------------------------------------------------


def test_a_name_a_module_binds_inside_a_block_is_bound_at_module_level() -> None:
    text = "try:\n    from fast import blob\nexcept ImportError:\n    blob = None\n"

    assert "blob" in {name for _, name in specialize._bindings(text)}


def test_an_import_the_destination_already_spells_differently_stops_the_copy() -> None:
    stamp = (Import("support", "stamp"),)

    assert specialize._clashes("from other_support import stamp\n", stamp, ())
    assert specialize._clashes("stamp = 1\n", stamp, ())
    assert specialize._clashes("json = 1\n", (), ("json",))
    assert not specialize._clashes("from support import stamp\n", stamp, ())
    assert not specialize._clashes("import json\n", (), ("json",))
    assert not specialize._clashes("", stamp, ("json",))
