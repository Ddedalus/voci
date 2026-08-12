"""Tests for velox._marks: mark accumulation, `marks_of`, and `parametrize` validation."""

from __future__ import annotations

import pytest
import velox
from velox._marks import marks_of


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
    assert marks_of(Derived) == velox.Marks()


def test_marks_of_never_raises_on_dict_less_objects() -> None:
    assert marks_of(object()) == velox.Marks()
    assert marks_of(42) == velox.Marks()


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
