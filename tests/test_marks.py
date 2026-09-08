"""Tests for voci._marks: mark accumulation, `marks_of`, and `parametrize` validation."""

from __future__ import annotations

import pytest

import voci
from voci._marks import Marks, decided, marks_of, merged


@pytest.mark.parametrize(
    "mark_name, first_arg, second_arg",
    [
        ("skip", "first", "second"),
        ("timeout", 10, 20),
        ("xfail", "flaky", "flaky again"),
    ],
)
def test_repeated_mark_raises_instead_of_overwriting(
    mark_name: str, first_arg: object, second_arg: object
) -> None:
    mark = getattr(voci, mark_name)

    @mark(first_arg)
    def target() -> None: ...

    with pytest.raises(TypeError, match=mark_name):
        mark(second_arg)(target)


def test_accumulating_marks_still_stack_freely() -> None:
    """skipifs/tags/parametrizations legitimately stack — only scalar marks are guarded."""

    @voci.skipif(True, reason="a")
    @voci.skipif(False, reason="b")
    @voci.tag("x")
    @voci.tag("y")
    def target() -> None: ...

    marks = marks_of(target)
    assert len(marks.skipifs) == 2
    assert marks.tags == ("y", "x")


def test_marks_of_does_not_walk_the_mro() -> None:
    """`getattr` would let a subclass inherit its base's marks; `marks_of` must read the
    object's own `__dict__` only."""

    @voci.skip("base only")
    class Base:
        pass

    class Derived(Base):
        pass

    assert marks_of(Base).skip is not None
    assert marks_of(Derived) == Marks()


def test_marks_of_never_raises_on_dict_less_objects() -> None:
    assert marks_of(object()) == Marks()
    assert marks_of(42) == Marks()


def test_parametrize_rejects_empty_names() -> None:
    with pytest.raises(ValueError, match="no argument names"):
        voci.parametrize("", [])


def test_parametrize_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        voci.parametrize("a,a", [(1, 2)])


def test_parametrize_rejects_empty_argvalues() -> None:
    """A `@parametrize` that contributes zero cases would otherwise expand into zero records --
    the test silently vanishing from the suite with no error, no skip, and nothing collected in
    its place."""
    with pytest.raises(ValueError, match="no argvalues"):
        voci.parametrize("n", [])


def test_parametrize_rejects_a_stale_ids_length() -> None:
    with pytest.raises(ValueError, match="id"):
        voci.parametrize("n", [1, 2, 3], ids=["one", "two"])


def test_parametrize_accepts_a_matching_ids_length() -> None:
    @voci.parametrize("n", [1, 2], ids=["one", "two"])
    def target(n: int) -> None: ...

    (param_set,) = marks_of(target).parametrizations
    assert param_set.ids == ("one", "two")


# `voci.case`: marks that reach one case of a `@parametrize`.
# ------------------------------------------------------------------------------------------


def test_case_reads_its_marks_from_the_decorators_a_test_would_carry() -> None:
    case = voci.case(2, marks=[voci.xfail("known"), voci.tag("slow")])

    assert case.values == (2,)
    assert case.marks.xfail is not None
    assert case.marks.xfail.reason == "known"
    assert case.marks.tags == ("slow",)


def test_case_accepts_one_decorator_as_well_as_a_sequence() -> None:
    assert voci.case(1, marks=voci.skip("just this one")).marks.skip is not None


def test_case_without_marks_is_the_bare_value() -> None:
    assert voci.case(1).marks == Marks()


def test_parametrize_records_case_marks_aligned_with_its_values() -> None:
    @voci.parametrize("n", [1, voci.case(2, marks=voci.skip("flaky"))])
    def target(n: int) -> None: ...

    (param_set,) = marks_of(target).parametrizations
    assert param_set.argvalues == ((1,), (2,))
    assert [bool(marks.skip) for marks in param_set.case_marks] == [False, True]


def test_parametrize_leaves_case_marks_empty_when_no_case_carries_any() -> None:
    """The alignment every reader of a `ParamSet` would otherwise have to keep in step is not
    built for the common case, where no case is marked at all."""

    @voci.parametrize("n", [1, 2])
    def target(n: int) -> None: ...

    (param_set,) = marks_of(target).parametrizations
    assert param_set.case_marks == ()


def test_case_spells_out_one_value_per_argname() -> None:
    @voci.parametrize("n,expected", [voci.case(2, 4, marks=voci.tag("slow")), (3, 9)])
    def target(n: int, expected: int) -> None: ...

    (param_set,) = marks_of(target).parametrizations
    assert param_set.argvalues == ((2, 4), (3, 9))


def test_case_holding_the_wrong_number_of_values_is_rejected() -> None:
    with pytest.raises(ValueError, match="2 value"):
        voci.parametrize("n,expected", [voci.case(2, marks=voci.tag("slow"))])


def test_case_rejects_a_decorator_that_attaches_no_voci_mark() -> None:
    """A `pytest.mark.*` hands the same function back, unmarked as far as voci is concerned --
    silently dropping it would leave the case looking marked and running unmarked."""
    with pytest.raises(TypeError, match="attached no voci marks"):
        voci.case(1, marks=pytest.mark.skip)


def test_case_rejects_a_decorator_that_replaces_the_function() -> None:
    def wrapping(fn: object) -> object:
        return lambda: None

    with pytest.raises(TypeError, match="not a voci mark decorator"):
        voci.case(1, marks=wrapping)


def test_case_rejects_a_parametrize_of_its_own() -> None:
    with pytest.raises(TypeError, match="whole test"):
        voci.case(1, marks=voci.parametrize("n", [1, 2]))


def test_case_marks_are_validated_where_the_case_is_written() -> None:
    """The decorators do their own validation, so a case's marks are rejected at decoration
    time rather than becoming a collection error later."""
    with pytest.raises(TypeError, match="twice"):
        voci.case(1, marks=[voci.skip("first"), voci.skip("second")])


# `@voci.xfail(condition=...)`: an expectation that only holds sometimes.
# ------------------------------------------------------------------------------------------


def test_xfail_records_its_condition() -> None:
    @voci.xfail("only on 3.14", condition=False)
    def target() -> None: ...

    marks = marks_of(target)
    assert marks.xfail is not None
    assert marks.xfail.condition is False


def test_xfail_condition_defaults_to_always() -> None:
    @voci.xfail("broken")
    def target() -> None: ...

    marks = marks_of(target)
    assert marks.xfail is not None
    assert marks.xfail.condition is True


def test_decided_drops_an_expectation_whose_condition_does_not_hold() -> None:
    @voci.xfail("only on windows", condition=lambda: False)
    def target() -> None: ...

    assert decided(marks_of(target)).xfail is None


def test_decided_keeps_an_expectation_whose_condition_holds() -> None:
    @voci.xfail("always", condition=lambda: True)
    def target() -> None: ...

    kept = decided(marks_of(target)).xfail
    assert kept is not None
    assert kept.reason == "always"


def test_merged_lets_a_case_stand_in_for_the_test_where_only_one_mark_can() -> None:
    """A case's own `skip`/`xfail`/`timeout` is the specific one, so it wins; the marks that
    accumulate across stacked decorators accumulate here too."""
    base = marks_of(_marked_test)
    extra = voci.case(1, marks=[voci.xfail("case"), voci.tag("case-tag")]).marks

    folded = merged(base, extra)

    assert folded.xfail is not None
    assert folded.xfail.reason == "case"
    assert folded.tags == ("test-tag", "case-tag")
    assert folded.timeout == 5
    assert folded.parametrizations == base.parametrizations


@voci.tag("test-tag")
@voci.timeout(5)
@voci.xfail("test")
def _marked_test() -> None: ...


def test_filterwarnings_accumulates_outermost_last() -> None:
    """The tuple's order is its precedence: later specs win, and decorators apply bottom-up,
    so the outermost one lands last and outranks the ones under it."""

    @voci.filterwarnings("error::DeprecationWarning")
    @voci.filterwarnings("ignore::UserWarning", "once::ResourceWarning")
    def target() -> None: ...

    assert marks_of(target).filterwarnings == (
        "ignore::UserWarning",
        "once::ResourceWarning",
        "error::DeprecationWarning",
    )


def test_filterwarnings_rejects_a_malformed_spec_where_it_was_written() -> None:
    with pytest.raises(ValueError, match="unknown action"):
        voci.filterwarnings("nope::UserWarning")


def test_a_cases_filters_are_folded_in_after_the_tests_own() -> None:
    @voci.filterwarnings("ignore::UserWarning")
    def target() -> None: ...

    folded = merged(marks_of(target), voci.case(1, marks=voci.filterwarnings("error")).marks)

    assert folded.filterwarnings == ("ignore::UserWarning", "error")
