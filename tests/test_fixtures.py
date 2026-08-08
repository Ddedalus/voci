"""Regression tests for velox._fixtures (spec/01 §3)."""

from __future__ import annotations

from typing import Annotated

import pytest
import velox
from velox import Depends
from velox._fixtures import (
    DIError,
    Injection,
    _check_acyclic,
    _check_missing_injections,
    plan_for,
)


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


# ------------------------------------------------------------------------------------------
# `plan_for` (spec/04 §1-2) — M1.
# ------------------------------------------------------------------------------------------


def test_plan_for_orders_steps_dependency_before_dependent() -> None:
    @velox.fixture()
    def c() -> str:
        return "c"

    @velox.fixture()
    def b(x: str = Depends(c)) -> str:
        return f"b+{x}"

    async def test_func(y: str = Depends(b)) -> None:
        pass

    plan = plan_for(test_func)

    assert [step.fixture.name for step in plan.steps] == ["c", "b"]
    # `b` is step_id 1 (built second); the test's own `y` resolves to it.
    assert plan.root_args == (("y", 1, False),)
    assert plan.steps[1].args == (("x", 0, False),)  # `b`'s own `x` resolves to `c`'s step_id 0


def test_plan_for_deduplicates_a_diamond_into_one_step() -> None:
    """B and C both depending on D: D is built once, not twice, and `test_func`'s two distinct
    `Depends()` sites (`p`, `q`) both resolve to the same steps for `b`/`c`."""

    @velox.fixture()
    def d() -> str:
        return "d"

    @velox.fixture()
    def b(x: str = Depends(d)) -> str:
        return x

    @velox.fixture()
    def c(x: str = Depends(d)) -> str:
        return x

    async def test_func(p: str = Depends(b), q: str = Depends(c)) -> None:
        pass

    plan = plan_for(test_func)

    names = [step.fixture.name for step in plan.steps]
    assert names.count("d") == 1
    assert len(plan.steps) == 3  # d, b, c — not four


def test_plan_for_never_deduplicates_call_scope_even_in_a_diamond() -> None:
    """The one deliberate exception to deduplication (module docstring): a `scope="call"`
    fixture reached by two paths still gets two independent steps."""

    @velox.fixture(scope="call")
    def d() -> object:
        return object()

    @velox.fixture()
    def b(x: object = Depends(d)) -> object:
        return x

    @velox.fixture()
    def c(x: object = Depends(d)) -> object:
        return x

    async def test_func(p: object = Depends(b), q: object = Depends(c)) -> None:
        pass

    plan = plan_for(test_func)

    names = [step.fixture.name for step in plan.steps]
    assert names.count("d") == 2
    assert len(plan.steps) == 4  # d, d, b, c


def test_plan_for_rejects_a_session_fixture_depending_on_a_function_fixture() -> None:
    """spec/04 §2's canonical scope-compatibility example, with both offending names checked in
    the message — the diagnostic is useless without them."""

    @velox.fixture(name="narrow_fx")
    def narrow() -> int:
        return 1

    @velox.fixture(scope="session", name="wide_fx")
    def wide(x: int = Depends(narrow)) -> int:
        return x

    async def test_func(w: int = Depends(wide)) -> None:
        pass

    with pytest.raises(DIError) as excinfo:
        plan_for(test_func)

    message = str(excinfo.value)
    assert "narrow_fx" in message
    assert "wide_fx" in message


def test_plan_for_rejects_a_module_fixture_depending_on_a_call_fixture() -> None:
    @velox.fixture(scope="call", name="per_call")
    def per_call() -> int:
        return 1

    @velox.fixture(scope="module", name="per_module")
    def per_module(x: int = Depends(per_call)) -> int:
        return x

    async def test_func(w: int = Depends(per_module)) -> None:
        pass

    with pytest.raises(DIError) as excinfo:
        plan_for(test_func)

    message = str(excinfo.value)
    assert "per_call" in message
    assert "per_module" in message


def test_plan_for_raises_on_a_missing_injection_naming_the_parameter() -> None:
    async def test_func(missing_param: int, present: int = Depends(alpha)) -> None:
        pass

    with pytest.raises(DIError, match="missing_param"):
        plan_for(test_func)


def test_plan_for_on_a_function_with_no_dependencies_is_a_trivially_empty_plan() -> None:
    async def test_func() -> None:
        pass

    plan = plan_for(test_func)
    assert plan.steps == ()
    assert plan.root_args == ()


def test_fixture_with_a_missing_injection_is_rejected_at_decoration_time() -> None:
    """Not just the test function: `_check_missing_injections` now runs the moment any `Fixture`
    is built (`Fixture.__init__`), so a fixture with a required, non-injected parameter is caught
    as soon as it's decorated — before any test ever reaches it via `Depends(...)`, and even if
    no test ever does."""
    with pytest.raises(DIError, match="conn"):

        @velox.fixture()
        def needs_arg(conn: object) -> object:
            return conn


def test_depends_on_a_positional_only_parameter_is_rejected_at_decoration_time() -> None:
    """`_di.setup`/`_construct` bind every injection by keyword; a positional-only parameter can
    never receive one, so this is caught statically instead of surfacing as an opaque `TypeError`
    at construction time."""
    with pytest.raises(DIError, match="x"):

        @velox.fixture()
        def outer(x: int = Depends(alpha), /) -> int:
            return x


# ------------------------------------------------------------------------------------------
# `_check_missing_injections` directly: the `co_varnames` slicing it relies on is subtle enough
# to break silently under an edit, so each of its cases gets its own pin.
# ------------------------------------------------------------------------------------------


def test_check_missing_injections_allows_star_args_and_star_kwargs() -> None:
    def t(*args: object, **kwargs: object) -> None:
        pass

    _check_missing_injections(t, ())  # must not raise


def test_check_missing_injections_reports_a_required_positional_before_star_args() -> None:
    def t(a: object, *args: object) -> None:
        pass

    with pytest.raises(DIError, match="a"):
        _check_missing_injections(t, ())


def test_check_missing_injections_reports_a_keyword_only_parameter_with_no_default() -> None:
    def t(*, a: object) -> None:
        pass

    with pytest.raises(DIError, match="a"):
        _check_missing_injections(t, ())


def test_check_missing_injections_allows_a_keyword_only_parameter_with_a_default() -> None:
    def t(*, a: object = 1) -> None:
        pass

    _check_missing_injections(t, ())  # must not raise


def test_check_missing_injections_skips_self_only_in_positional_position() -> None:
    def positional_self(self: object) -> None:
        pass

    _check_missing_injections(positional_self, ())  # must not raise

    def keyword_only_self(*, self: object) -> None:
        pass

    with pytest.raises(DIError, match="self"):
        _check_missing_injections(keyword_only_self, ())
