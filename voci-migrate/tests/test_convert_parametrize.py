"""Tests for voci_migrate.convert.parametrize: reading a case list, and deciding where it goes.

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

from voci_migrate import model
from voci_migrate.convert import parametrize
from voci_migrate.convert.parametrize import Decision
from voci_migrate.model import GroundTruth

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
DUMPS = CORPUS / "dumps"

PARAMETRIZE = "parametrize_showcase"
FIXTURES = "fixtures_showcase"
MECHANICAL = "mechanical_showcase"


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


def test_two_stacked_marks_over_plain_arguments_stay_two_axes() -> None:
    # pytest folds every "direct" (non-fixture) parametrized name's own index into one counter
    # shared by the whole callspec once two or more such axes stack on a test
    # (`Metafunc._recompute_direct_params_indices`), so `outer` and `inner` here carry the same
    # index in every one of the four cases even though neither is parametrized with the other's
    # values. Grouping by that alone would fuse them into one axis that explains no position of
    # the composed id at all.
    gt = ground_truth(MECHANICAL)
    cases = [item for item in gt.items if item.originalname == "test_parametrize_stacked"]
    for case in cases:
        assert case.callspec is not None
        assert case.callspec.indices["outer"] == case.callspec.indices["inner"]

    inner, outer = sorted(parametrize.axes(cases), key=lambda axis: axis.key)

    assert outer.argnames == ("outer",)
    assert outer.values == (("'o1'",), ("'o2'",))
    assert outer.ids == ("o1", "o2")
    assert inner.argnames == ("inner",)
    assert inner.values == (("'i1'",), ("'i2'",))
    assert inner.ids == ("i1", "i2")


def test_a_repeated_value_keeps_its_own_case() -> None:
    # A lone axis is never touched by the direct-param fold `test_two_stacked_marks_...` covers
    # above, so its own `indices` still tells two cases with the same value apart. Keying the
    # recovered position on the repr of the row instead — value-based identity, needed only where
    # the fold actually reaches — would fold the second `True` case onto the first and silently
    # drop it.
    gt = ground_truth(MECHANICAL)
    cases = [item for item in gt.items if item.originalname == "test_parametrize_repeated_value"]
    assert len(cases) == 3

    (axis,) = parametrize.axes(cases)

    assert axis.argnames == ("flag",)
    assert axis.values == (("True",), ("False",), ("True",))
    assert axis.ids == ("True0", "False", "True1")


def test_a_hook_axis_stacked_with_a_mark_stays_its_own_axis() -> None:
    # The direct-param fold `test_two_stacked_marks_...` covers isn't specific to
    # `@pytest.mark.parametrize` -- a `pytest_generate_tests` hook parametrizing a plain name with
    # no fixture behind it is exactly as "direct" to pytest as a mark is, so stacking it with
    # another mark folds its own index into the shared counter too. Edited from
    # `test_parametrize_stacked`'s own dump (real pytest-produced indices, `test_two_stacked_marks_
    # ...` above): `inner`'s mark is stripped and its fixturedef swapped for a
    # `DirectParamFixtureDef`, simulating a hook building it instead of a mark -- the fold pytest
    # already recorded for these four cases doesn't care which, and is untouched. Before
    # `_direct_names` recognised this shape, `inner`'s raw (folded) `indices` reported four
    # single-case values instead of the two it actually has.
    dump = json.loads((DUMPS / f"{MECHANICAL}-pytest-9.1.json").read_text(encoding="utf-8"))
    cases = [
        entry
        for entry in dump["items"]
        if entry["nodeid"].startswith("test_marks.py::test_parametrize_stacked")
    ]
    assert len(cases) == 4
    dump["fixture_defs"]["hook_inner"] = {
        "argname": "inner",
        "scope": "function",
        "params": None,
        "ids": None,
        "autouse": False,
        "visibility": "",
        "kind": "DirectParamFixtureDef",
        "direct_param": True,
        "argnames": ["request"],
        "returns": "Any",
        "func": {
            "module": "_pytest.python",
            "qualname": "get_direct_param_fixture_func",
            "file": "${site_packages}/_pytest/python.py",
            "lineno": 1154,
            "wrapped": False,
        },
    }
    for entry in cases:
        entry["own_markers"] = [m for m in entry["own_markers"] if m["args"][:1] != ["'inner'"]]
        entry["markers_with_origin"] = [
            m for m in entry["markers_with_origin"] if m["args"][:1] != ["'inner'"]
        ]
        entry["name2fixturedefs"]["inner"] = ["hook_inner"]

    gt = model.build(dump)
    cases_built = [item for item in gt.items if item.originalname == "test_parametrize_stacked"]
    for case in cases_built:
        assert case.callspec is not None
        assert case.callspec.indices["outer"] == case.callspec.indices["inner"]

    inner, outer = sorted(parametrize.axes(cases_built), key=lambda axis: axis.key)

    assert outer.argnames == ("outer",)
    assert outer.values == (("'o1'",), ("'o2'",))
    assert inner.argnames == ("inner",)
    assert inner.values == (("'i1'",), ("'i2'",))


def test_two_stacked_hook_axes_stay_two_axes() -> None:
    # The sibling gap `test_a_hook_axis_stacked_with_a_mark_stays_its_own_axis` didn't reach:
    # both `outer` and `inner` built by hooks, with no mark on either to fall back on. Both fold
    # to the same raw index (asserted below, as that test does) *and* neither has a
    # `_mark_positions` entry, so `_grouped` used to key both on that identical raw-index tuple
    # and fuse them into one `("inner", "outer")` group. Edited the same way as the hook+mark
    # test above, but stripping both marks: every `(inner, outer)` pair actually occurs across the
    # four cases, so `_hook_grouped` reads that as the independent product two separate axes would
    # produce, not the fewer-than-the-product pairing one shared axis would leave.
    dump = json.loads((DUMPS / f"{MECHANICAL}-pytest-9.1.json").read_text(encoding="utf-8"))
    cases = [
        entry
        for entry in dump["items"]
        if entry["nodeid"].startswith("test_marks.py::test_parametrize_stacked")
    ]
    assert len(cases) == 4
    for name in ("inner", "outer"):
        dump["fixture_defs"][f"hook_{name}"] = {
            "argname": name,
            "scope": "function",
            "params": None,
            "ids": None,
            "autouse": False,
            "visibility": "",
            "kind": "DirectParamFixtureDef",
            "direct_param": True,
            "argnames": ["request"],
            "returns": "Any",
            "func": {
                "module": "_pytest.python",
                "qualname": "get_direct_param_fixture_func",
                "file": "${site_packages}/_pytest/python.py",
                "lineno": 1154,
                "wrapped": False,
            },
        }
    for entry in cases:
        entry["own_markers"] = [
            m for m in entry["own_markers"] if m["args"][:1] not in (["'inner'"], ["'outer'"])
        ]
        entry["markers_with_origin"] = [
            m
            for m in entry["markers_with_origin"]
            if m["args"][:1] not in (["'inner'"], ["'outer'"])
        ]
        entry["name2fixturedefs"]["inner"] = ["hook_inner"]
        entry["name2fixturedefs"]["outer"] = ["hook_outer"]

    gt = model.build(dump)
    cases_built = [item for item in gt.items if item.originalname == "test_parametrize_stacked"]
    for case in cases_built:
        assert case.callspec is not None
        assert case.callspec.indices["outer"] == case.callspec.indices["inner"]

    inner, outer = sorted(parametrize.axes(cases_built), key=lambda axis: axis.key)

    assert outer.argnames == ("outer",)
    assert outer.values == (("'o1'",), ("'o2'",))
    assert inner.argnames == ("inner",)
    assert inner.values == (("'i1'",), ("'i2'",))


def test_a_composite_hook_axis_with_a_repeated_column_stays_one_axis() -> None:
    # `width`/`height` (`test_an_axis_carries_its_values_ids_and_place_in_the_composed_id` above)
    # is one hook call's own composite axis, its two dumped rows -- (2, 3), (5, 8) -- never
    # repeating either column's value. A third case reusing `width`'s first value alongside
    # `height`'s second -- (2, 8) -- repeats a value in each column without the rows themselves
    # repeating: `width` alone now reads 2, 5, 2 and `height` alone 3, 8, 8, so a recovery keyed on
    # each column's own distinct values would tell them apart instead of pairing them.
    # `_hook_grouped` reads the pair instead: (2, 3), (5, 8), (2, 8) are three distinct pairs,
    # fewer than the four `2 * 2` distinct values in each column would produce if the columns
    # varied independently, so they stay one axis.
    dump = json.loads((DUMPS / f"{PARAMETRIZE}-pytest-9.1.json").read_text(encoding="utf-8"))
    large = next(
        entry for entry in dump["items"] if entry["nodeid"] == "test_generated.py::test_area[large]"
    )
    third = json.loads(json.dumps(large))
    third["nodeid"] = "test_generated.py::test_area[medium]"
    third["callspec"] = {
        "id": "medium",
        "idlist": ["medium"],
        "params": {"width": "2", "height": "8"},
        "indices": {"width": 2, "height": 2},
        "marks": [],
    }
    dump["items"].append(third)

    gt = model.build(dump)
    cases = [item for item in gt.items if item.originalname == "test_area"]
    assert len(cases) == 3

    (axis,) = parametrize.axes(cases)

    assert axis.argnames == ("width", "height")
    assert axis.values == (("2", "3"), ("5", "8"), ("2", "8"))


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
    assert finding.code == "VC029"
    assert "already has cases of its own" in finding.message
    assert finding.tests == ("test_top.py::test_indirect[mysql]",)
    assert decided.carried == {}


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        pytest.param("values", "different values", id="disagreeing-values"),
        pytest.param("reach", "without parametrizing it", id="unparametrized-consumer"),
        pytest.param("group", "the test's own decorator", id="mark-on-a-group"),
    ],
)
def test_an_indirect_mark_a_params_fixture_cannot_answer_for_is_refused(
    edit: str, expected: str
) -> None:
    # Three shapes a green pytest suite can hold that one case list cannot: the same fixture given
    # different values by two tests, a test reaching it that named no values at all — which would
    # silently gain the cases the other tests chose — and a mark written for a group of tests,
    # where the rewrite has no decorator on the test to take away.
    dump = json.loads((DUMPS / f"{PARAMETRIZE}-pytest-9.1.json").read_text(encoding="utf-8"))
    through = [
        entry
        for entry in dump["items"]
        if entry["nodeid"].startswith("test_indirect.py::test_backend_through_engine")
    ]
    if edit == "values":
        through[0]["callspec"]["params"]["backend"] = "'postgres'"
    elif edit == "group":
        for entry in dump["items"]:
            for mark in entry["markers_with_origin"]:
                if mark["name"] == "parametrize":
                    mark["from"] = "test_indirect.py"
    else:
        dump["items"].remove(through[1])
        through[0]["callspec"] = None
        through[0]["nodeid"] = "test_indirect.py::test_backend_through_engine"

    decided = decision(model.build(dump))

    assert {finding.code for finding in decided.findings} == {"VC029"}
    assert any(expected in finding.message for finding in decided.findings)
    assert "backend" not in carried_by(model.build(dump), decided)
