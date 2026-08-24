"""Tests for velox._marks: mark accumulation, `marks_of`, and `parametrize` validation."""

from __future__ import annotations

import pytest

import velox
from velox._marks import Marks, decided, marks_of, merged


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
    mark = getattr(velox, mark_name)

    @mark(first_arg)
    def target() -> None: ...

    with pytest.raises(TypeError, match=mark_name):
        mark(second_arg)(target)


def test_accumulating_marks_still_stack_freely() -> None:
    """skipifs/tags/parametrizations legitimately stack — only scalar marks are guarded."""

    @velox.skipif(True, reason="a")
    @velox.skipif(False, reason="b")
    @velox.tag("x")
    @velox.tag("y")
    def target() -> None: ...

    marks = marks_of(target)
    assert len(marks.skipifs) == 2
    assert marks.tags == ("y", "x")


def test_marks_of_does_not_walk_the_mro() -> None:
    """`getattr` would let a subclass inherit its base's marks; `marks_of` must read the
    object's own `__dict__` only."""

    @velox.skip("base only")
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
        velox.parametrize("", [])


def test_parametrize_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        velox.parametrize("a,a", [(1, 2)])


def test_parametrize_rejects_empty_argvalues() -> None:
    """A `@parametrize` that contributes zero cases would otherwise expand into zero records --
    the test silently vanishing from the suite with no error, no skip, and nothing collected in
    its place."""
    with pytest.raises(ValueError, match="no argvalues"):
        velox.parametrize("n", [])


def test_parametrize_rejects_a_stale_ids_length() -> None:
    with pytest.raises(ValueError, match="id"):
        velox.parametrize("n", [1, 2, 3], ids=["one", "two"])


def test_parametrize_accepts_a_matching_ids_length() -> None:
    @velox.parametrize("n", [1, 2], ids=["one", "two"])
    def target(n: int) -> None: ...

    (param_set,) = marks_of(target).parametrizations
    assert param_set.ids == ("one", "two")


# `velox.case`: marks that reach one case of a `@parametrize`.
# ------------------------------------------------------------------------------------------


def test_case_reads_its_marks_from_the_decorators_a_test_would_carry() -> None:
    case = velox.case(2, marks=[velox.xfail("known"), velox.tag("slow")])

    assert case.values == (2,)
    assert case.marks.xfail is not None
    assert case.marks.xfail.reason == "known"
    assert case.marks.tags == ("slow",)


def test_case_accepts_one_decorator_as_well_as_a_sequence() -> None:
    assert velox.case(1, marks=velox.skip("just this one")).marks.skip is not None


def test_case_without_marks_is_the_bare_value() -> None:
    assert velox.case(1).marks == Marks()


def test_parametrize_records_case_marks_aligned_with_its_values() -> None:
    @velox.parametrize("n", [1, velox.case(2, marks=velox.skip("flaky"))])
    def target(n: int) -> None: ...

    (param_set,) = marks_of(target).parametrizations
    assert param_set.argvalues == ((1,), (2,))
    assert [bool(marks.skip) for marks in param_set.case_marks] == [False, True]


def test_parametrize_leaves_case_marks_empty_when_no_case_carries_any() -> None:
    """The alignment every reader of a `ParamSet` would otherwise have to keep in step is not
    built for the common case, where no case is marked at all."""

    @velox.parametrize("n", [1, 2])
    def target(n: int) -> None: ...

    (param_set,) = marks_of(target).parametrizations
    assert param_set.case_marks == ()


def test_case_spells_out_one_value_per_argname() -> None:
    @velox.parametrize("n,expected", [velox.case(2, 4, marks=velox.tag("slow")), (3, 9)])
    def target(n: int, expected: int) -> None: ...

    (param_set,) = marks_of(target).parametrizations
    assert param_set.argvalues == ((2, 4), (3, 9))


def test_case_holding_the_wrong_number_of_values_is_rejected() -> None:
    with pytest.raises(ValueError, match="2 value"):
        velox.parametrize("n,expected", [velox.case(2, marks=velox.tag("slow"))])


def test_case_rejects_a_decorator_that_attaches_no_velox_mark() -> None:
    """A `pytest.mark.*` hands the same function back, unmarked as far as velox is concerned --
    silently dropping it would leave the case looking marked and running unmarked."""
    with pytest.raises(TypeError, match="attached no velox marks"):
        velox.case(1, marks=pytest.mark.skip)


def test_case_rejects_a_decorator_that_replaces_the_function() -> None:
    def wrapping(fn: object) -> object:
        return lambda: None

    with pytest.raises(TypeError, match="not a velox mark decorator"):
        velox.case(1, marks=wrapping)


def test_case_rejects_a_parametrize_of_its_own() -> None:
    with pytest.raises(TypeError, match="whole test"):
        velox.case(1, marks=velox.parametrize("n", [1, 2]))


def test_case_marks_are_validated_where_the_case_is_written() -> None:
    """The decorators do their own validation, so a case's marks are rejected at decoration
    time rather than becoming a collection error later."""
    with pytest.raises(TypeError, match="twice"):
        velox.case(1, marks=[velox.skip("first"), velox.skip("second")])


# `@velox.xfail(condition=...)`: an expectation that only holds sometimes.
# ------------------------------------------------------------------------------------------


def test_xfail_records_its_condition() -> None:
    @velox.xfail("only on 3.14", condition=False)
    def target() -> None: ...

    marks = marks_of(target)
    assert marks.xfail is not None
    assert marks.xfail.condition is False


def test_xfail_condition_defaults_to_always() -> None:
    @velox.xfail("broken")
    def target() -> None: ...

    marks = marks_of(target)
    assert marks.xfail is not None
    assert marks.xfail.condition is True


def test_decided_drops_an_expectation_whose_condition_does_not_hold() -> None:
    @velox.xfail("only on windows", condition=lambda: False)
    def target() -> None: ...

    assert decided(marks_of(target)).xfail is None


def test_decided_keeps_an_expectation_whose_condition_holds() -> None:
    @velox.xfail("always", condition=lambda: True)
    def target() -> None: ...

    kept = decided(marks_of(target)).xfail
    assert kept is not None
    assert kept.reason == "always"


def test_merged_lets_a_case_stand_in_for_the_test_where_only_one_mark_can() -> None:
    """A case's own `skip`/`xfail`/`timeout` is the specific one, so it wins; the marks that
    accumulate across stacked decorators accumulate here too."""
    base = marks_of(_marked_test)
    extra = velox.case(1, marks=[velox.xfail("case"), velox.tag("case-tag")]).marks

    folded = merged(base, extra)

    assert folded.xfail is not None
    assert folded.xfail.reason == "case"
    assert folded.tags == ("test-tag", "case-tag")
    assert folded.timeout == 5
    assert folded.parametrizations == base.parametrizations


@velox.tag("test-tag")
@velox.timeout(5)
@velox.xfail("test")
def _marked_test() -> None: ...
