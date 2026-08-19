"""Tests for velox_migrate.convert.parametrize: reading a case list, and deciding where it goes.

The corpus suite is the subject where one exists — `parametrize_showcase` for what converts and
`fixtures_showcase` for the fixture that has cases of its own — and an edited dump for the shapes
no green pytest suite would be written in. What matters here is the decision, not the source it
becomes: whether a fixture can carry the values its call sites gave it is a question about every
test in the suite, and asking it wrong is how a whole subtree ends up running the wrong cases.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import libcst as cst

import pytest
from velox_migrate import model
from velox_migrate.convert import parametrize
from velox_migrate.convert.parametrize import Decision
from velox_migrate.model import GroundTruth

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
DUMPS = CORPUS / "dumps"

PARAMETRIZE = "parametrize_showcase"
FIXTURES = "fixtures_showcase"


def ground_truth(suite: str) -> GroundTruth:
    return model.load(DUMPS / f"{suite}-pytest-9.1.json")


def decision(gt: GroundTruth) -> Decision:
    items: dict[tuple[str, str], list[model.Item]] = {}
    for item in gt.items:
        qualname = f"{item.cls}.{item.originalname}" if item.cls else item.originalname
        items.setdefault((item.path or "", qualname), []).append(item)
    translatable = {
        key: fixture
        for key, fixture in gt.fixture_defs.items()
        if fixture.func.file is not None and not fixture.func.file.startswith("${")
    }
    return parametrize.decide(
        gt,
        items={site: tuple(cases) for site, cases in items.items()},
        translatable=translatable,
    )


def carried_by(gt: GroundTruth, decided: Decision) -> Mapping[str, parametrize.Carried]:
    return {gt.fixture_defs[key].argname: value for key, value in decided.carried.items()}


def rendered(node: cst.BaseExpression | None) -> str:
    assert node is not None
    return cst.Module(body=()).code_for_node(node)


# --- spelling a value -----------------------------------------------------------------------


def test_a_string_is_written_the_way_every_other_rule_writes_one() -> None:
    # The `repr` a dump carries is single-quoted, and the case list it becomes is read beside
    # decorators this tool wrote itself.
    assert rendered(parametrize.literal("'mysql'")) == '"mysql"'


def test_a_nested_literal_keeps_its_shape() -> None:
    assert rendered(parametrize.literal("{'a': (1, 2), 'b': [None, True]}")) == (
        '{"a": (1, 2), "b": [None, True]}'
    )


def test_a_set_is_written_in_one_order_however_the_dump_spelled_it() -> None:
    # A set has no source order, so two runs over the same dump have to write the same line.
    assert rendered(parametrize.literal("{'b', 'a'}")) == '{"a", "b"}'


def test_a_repr_that_is_not_a_literal_has_no_spelling() -> None:
    assert parametrize.literal("<Backend object at 0x1>") is None
    assert parametrize.literal("Backend(dsn='x')") is None


# --- reading the axes of one test -------------------------------------------------------------


def test_an_axis_carries_its_values_ids_and_place_in_the_composed_id() -> None:
    gt = ground_truth(PARAMETRIZE)
    cases = [item for item in gt.items if item.originalname == "test_area"]

    (axis,) = parametrize.axes(cases)

    assert axis.argnames == ("width", "height")
    assert axis.values == (("2", "3"), ("5", "8"))
    assert axis.ids == ("small", "large")
    assert axis.position == 0


def test_a_test_with_no_cases_has_no_axes() -> None:
    gt = ground_truth(FIXTURES)
    cases = [item for item in gt.items if item.originalname == "test_uses"]

    assert parametrize.axes(cases) == ()


# --- what each construct becomes ----------------------------------------------------------------


def test_the_values_of_an_indirect_mark_land_on_the_fixture_it_named() -> None:
    gt = ground_truth(PARAMETRIZE)

    decided = decision(gt)

    assert carried_by(gt, decided)["backend"] == parametrize.Carried(
        values=("'mysql'", "'sqlite'"), ids=("mysql", "sqlite")
    )
    assert decided.dropped[("test_indirect.py", "test_backend_directly")] == frozenset({"backend"})
    assert decided.findings == ()


def test_a_hook_built_axis_becomes_a_parametrize_listing_it() -> None:
    gt = ground_truth(PARAMETRIZE)

    decided = decision(gt)

    (axis,) = decided.generated[("test_generated.py", "test_letters")]
    assert axis.argnames == ("letter",)
    assert axis.values == (("'a'",), ("'b'",))
    assert axis.ids == ("a", "b")


def test_a_fixture_with_cases_of_its_own_cannot_take_a_call_sites_as_well() -> None:
    gt = ground_truth(FIXTURES)

    decided = decision(gt)

    (finding,) = decided.findings
    assert finding.code == "VX029"
    assert "already has cases of its own" in finding.message
    assert finding.tests == ("test_top.py::test_indirect[mysql]",)
    assert decided.carried == {}


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        pytest.param("values", "different values", id="disagreeing-values"),
        pytest.param("reach", "without parametrizing it", id="unparametrized-consumer"),
    ],
)
def test_an_indirect_mark_a_params_fixture_cannot_answer_for_is_refused(
    edit: str, expected: str
) -> None:
    # Two shapes a green pytest suite can hold that one case list cannot: the same fixture given
    # different values by two tests, and a test reaching it that named no values at all — which
    # would silently gain the cases the other tests chose.
    dump = json.loads((DUMPS / f"{PARAMETRIZE}-pytest-9.1.json").read_text(encoding="utf-8"))
    through = [
        entry
        for entry in dump["items"]
        if entry["nodeid"].startswith("test_indirect.py::test_backend_through_engine")
    ]
    if edit == "values":
        through[0]["callspec"]["params"]["backend"] = "'postgres'"
    else:
        dump["items"].remove(through[1])
        through[0]["callspec"] = None
        through[0]["nodeid"] = "test_indirect.py::test_backend_through_engine"

    decided = decision(model.build(dump))

    assert {finding.code for finding in decided.findings} == {"VX029"}
    assert any(expected in finding.message for finding in decided.findings)
    assert "backend" not in carried_by(model.build(dump), decided)
