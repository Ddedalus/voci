"""Tests for velox._collection.parametrize: callspec expansion and id generation."""

from __future__ import annotations

import enum

import pytest

import velox
from velox._collection.parametrize import cases_for, known_params_of
from velox._marks import NO_MARKS, ParamSet, marks_of


def test_known_params_of_is_the_union_of_every_paramsets_argnames() -> None:
    param_sets = (ParamSet(("outer",), ((1,), (2,))), ParamSet(("inner",), (("a",), ("b",))))
    assert known_params_of(param_sets) == frozenset({"outer", "inner"})


def test_known_params_of_is_empty_for_no_parametrizations() -> None:
    assert known_params_of(()) == frozenset()


def test_known_params_of_rejects_a_name_reused_across_stacked_paramsets() -> None:
    param_sets = (ParamSet(("n",), ((1,),)), ParamSet(("n",), ((2,),)))
    with pytest.raises(ValueError, match="n"):
        known_params_of(param_sets)


def test_cases_for_no_parametrizations_is_empty() -> None:
    assert cases_for(()) == ()


def test_cases_for_one_paramset_yields_one_case_per_value() -> None:
    param_sets = (ParamSet(("n",), ((1,), (2,))),)
    cases = cases_for(param_sets)
    assert [case.params for case in cases] == [{"n": 1}, {"n": 2}]
    assert [case.id for case in cases] == ["1", "2"]


def test_cases_for_multiple_argnames_joins_their_ids_with_a_dash() -> None:
    param_sets = (ParamSet(("n", "expected"), ((1, 2), (2, 4))),)
    cases = cases_for(param_sets)
    assert [case.params for case in cases] == [{"n": 1, "expected": 2}, {"n": 2, "expected": 4}]
    assert [case.id for case in cases] == ["1-2", "2-4"]


def test_cases_for_stacked_paramsets_is_the_cartesian_product_outermost_slowest() -> None:
    """Matches `Marks.parametrizations`' own contract: outermost (first) varies slowest."""
    param_sets = (ParamSet(("outer",), ((1,), (2,))), ParamSet(("inner",), (("a",), ("b",))))
    cases = cases_for(param_sets)
    assert [case.params for case in cases] == [
        {"outer": 1, "inner": "a"},
        {"outer": 1, "inner": "b"},
        {"outer": 2, "inner": "a"},
        {"outer": 2, "inner": "b"},
    ]
    assert [case.id for case in cases] == ["1-a", "1-b", "2-a", "2-b"]


def test_cases_for_a_paramset_with_no_argvalues_contributes_no_cases() -> None:
    assert cases_for((ParamSet(("n",), ()),)) == ()


class _Color(enum.Enum):
    RED = "red"


@pytest.mark.parametrize(
    "value, expected_id",
    [(True, "True"), (False, "False"), (None, "None"), ("x", "x"), (5, "5"), (_Color.RED, "RED")],
)
def test_auto_ids_use_the_literal_for_common_types(value: object, expected_id: str) -> None:
    (case,) = cases_for((ParamSet(("v",), ((value,),)),))
    assert case.id == expected_id


def test_auto_ids_fall_back_to_argname_and_index_for_other_types() -> None:
    param_sets = (ParamSet(("payload",), (({"a": 1},), ({"a": 2},))),)
    cases = cases_for(param_sets)
    assert [case.id for case in cases] == ["payload0", "payload1"]


def test_explicit_ids_tuple_is_used_verbatim() -> None:
    param_sets = (ParamSet(("n",), ((1,), (2,)), ids=("one", "two")),)
    cases = cases_for(param_sets)
    assert [case.id for case in cases] == ["one", "two"]


def test_explicit_ids_callable_overrides_the_auto_id_per_value() -> None:
    param_sets = (ParamSet(("n",), ((1,), (2,)), ids=lambda v: f"case-{v}" if v == 1 else None),)
    cases = cases_for(param_sets)
    # Value 1 -> the callable's own id; value 2 -> the callable returned None, so falls back.
    assert [case.id for case in cases] == ["case-1", "2"]


def test_explicit_ids_callable_that_raises_falls_back_to_the_auto_id() -> None:
    def broken(_: object) -> str:
        raise RuntimeError("boom")

    param_sets = (ParamSet(("n",), ((1,),), ids=broken),)
    (case,) = cases_for(param_sets)
    assert case.id == "1"


def test_colliding_generated_ids_are_disambiguated_with_an_occurrence_count() -> None:
    param_sets = (ParamSet(("n",), ((1,), (2,), (3,)), ids=lambda v: "same"),)
    cases = cases_for(param_sets)
    assert [case.id for case in cases] == ["same0", "same1", "same2"]


def test_dedupe_never_renames_into_an_id_another_case_already_uses() -> None:
    """`1, 1, "10"` auto-ids to `"1", "1", "10"`. Renaming the duplicate `"1"`s naively to
    `"10"`/`"11"` would collide with the third case's already-unique `"10"` -- the fix checks
    every rename against every id used so far, not just its own collision group."""
    param_sets = (ParamSet(("n",), ((1,), (1,), ("10",))),)
    cases = cases_for(param_sets)
    ids = [case.id for case in cases]
    assert ids == ["11", "12", "10"]
    assert len(set(ids)) == len(ids), f"duplicate id among {ids!r}"


def test_cases_for_carries_each_cases_own_marks() -> None:
    marked = ParamSet(("n",), ((1,), (2,)), case_marks=(NO_MARKS, marks_of(_skipped)))
    cases = cases_for((marked,))
    assert [case.marks.skip is not None for case in cases] == [False, True]


def test_cases_for_folds_the_marks_of_every_axis_of_one_combination() -> None:
    """A combination is one case of each stacked parametrization, so it carries the marks of
    each of them -- neither axis's marks are dropped because the other varies."""
    outer = ParamSet(("outer",), ((1,), (2,)), case_marks=(marks_of(_tagged), NO_MARKS))
    inner = ParamSet(("inner",), (("a",),), case_marks=(marks_of(_skipped),))
    cases = cases_for((outer, inner))
    assert [(case.marks.tags, case.marks.skip is not None) for case in cases] == [
        (("slow",), True),
        ((), True),
    ]


def test_cases_for_leaves_an_unmarked_paramsets_cases_unmarked() -> None:
    (case,) = cases_for((ParamSet(("n",), ((1,),)),))
    assert case.marks == NO_MARKS


@velox.skip("marked case")
def _skipped() -> None: ...


@velox.tag("slow")
def _tagged() -> None: ...
