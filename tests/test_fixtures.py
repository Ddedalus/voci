"""Regression tests for velox._fixtures (spec/01 §3)."""

from __future__ import annotations

from typing import Annotated

import pytest
import velox
from velox import Depends
from velox._fixtures import Injection, _check_acyclic


@velox.fixture()
def alpha() -> int:
    return 1


def test_annotated_depends_is_rejected_at_decoration_time() -> None:
    """`def t(db: Annotated[Session, Depends(fx)])` is the FastAPI spelling; velox only ever
    reads `__defaults__`/`__kwdefaults__` (spec/01 rule 3), so it would silently inject nothing.
    Caught here since nothing downstream ever will."""

    def bad(db: int = 0) -> int:
        return db

    # Set directly rather than written in the def: this test module has `from __future__ import
    # annotations`, which would stringify a literal `Annotated[...]` in the signature and defeat
    # the very check under test.
    bad.__annotations__["db"] = Annotated[int, Depends(alpha)]

    with pytest.raises(TypeError, match="Annotated"):
        velox.fixture()(bad)


def test_ordinary_annotated_types_are_left_alone() -> None:
    @velox.fixture()
    def fine(db: Annotated[int, "not a dependency"] = Depends(alpha)) -> int:
        return db

    (injection,) = fine.plan
    assert injection.param == "db"


def test_check_acyclic_detects_a_rigged_cycle() -> None:
    """A single `@velox.fixture()` decoration can never build a cycle — a dependency must
    already exist as an object before it can be depended on — so the detector is exercised
    directly against a graph rigged to loop."""
    a = velox.fixture()(lambda: 0)
    b = velox.fixture()(lambda: 0)
    a._plan = (Injection(param="b", source=b, keyword_only=False),)
    b._plan = (Injection(param="a", source=a, keyword_only=False),)

    with pytest.raises(ValueError, match="cycle"):
        _check_acyclic(a)


def test_check_acyclic_allows_a_diamond() -> None:
    """B and C both depending on D is not a cycle, just the same node reached twice."""
    d = velox.fixture()(lambda: 0)

    @velox.fixture()
    def b(x: int = Depends(d)) -> int:
        return x

    @velox.fixture()
    def c(x: int = Depends(d)) -> int:
        return x

    @velox.fixture()
    def top(p: int = Depends(b), q: int = Depends(c)) -> int:
        return p + q

    _check_acyclic(top)  # must not raise
