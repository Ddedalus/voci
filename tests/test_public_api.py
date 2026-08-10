"""Tests for velox's public API: fixture declaration, marks, parametrize, builtin fixtures,
and the `approx`/`raises` assertion helpers.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
import velox
from velox import Depends
from velox._marks import marks_of


@velox.fixture()
def alpha() -> int:
    return 1


@velox.fixture(scope="session", exclusive="db", name="the beta")
def beta() -> str:
    return "b"


@velox.fixture()
def gamma(a: int = Depends(alpha), b: str = Depends(beta)) -> str:
    return f"{a}{b}"


def test_fixture_carries_its_declaration() -> None:
    assert alpha.name == "alpha"
    assert alpha.scope == "function"
    assert alpha.exclusive is False

    assert beta.name == "the beta"
    assert beta.scope == "session"
    assert beta.exclusive == "db"


def test_plan_is_read_from_defaults() -> None:
    assert [i.param for i in gamma.plan] == ["a", "b"]
    assert gamma.dependencies == (alpha, beta)
    assert all(not i.keyword_only for i in gamma.plan)


def test_keyword_only_dependencies_are_found() -> None:
    @velox.fixture()
    def delta(*, a: int = Depends(alpha)) -> int:
        return a

    (injection,) = delta.plan
    assert injection.param == "a"
    assert injection.keyword_only is True


def test_ordinary_defaults_are_left_alone() -> None:
    @velox.fixture()
    def epsilon(a: int = Depends(alpha), retries: int = 3) -> int:
        return a * retries

    assert [i.param for i in epsilon.plan] == ["a"]


def test_generators_and_async_are_all_fixtures() -> None:
    @velox.fixture()
    async def agen() -> AsyncIterator[int]:
        yield 1

    @velox.fixture()
    async def coro() -> int:
        return 1

    assert isinstance(agen, velox.Fixture)
    assert isinstance(coro, velox.Fixture)


def test_marks_stack_into_one_record() -> None:
    @velox.solo
    @velox.tag("slow", "integration")
    @velox.timeout(30)
    @velox.xfail("upstream", strict=True, raises=TimeoutError)
    def target() -> None: ...

    marks = marks_of(target)
    assert marks.solo is True
    assert marks.tags == ("slow", "integration")
    assert marks.timeout == 30
    assert marks.xfail is not None
    assert marks.xfail.strict is True
    assert marks.xfail.raises is TimeoutError


def test_unmarked_function_has_an_empty_record() -> None:
    def target() -> None: ...

    assert marks_of(target) == velox.Marks()


def test_parametrize_normalizes_names_and_cases() -> None:
    @velox.parametrize("n,expected", [(1, 2), (2, 4)])
    def target(n: int, expected: int) -> None: ...

    (param_set,) = marks_of(target).parametrizations
    assert param_set.argnames == ("n", "expected")
    assert param_set.argvalues == ((1, 2), (2, 4))


def test_parametrize_wraps_single_name_values() -> None:
    @velox.parametrize("email", ["a@b.c", "d@e.f"])
    def target(email: str) -> None: ...

    (param_set,) = marks_of(target).parametrizations
    assert param_set.argvalues == (("a@b.c",), ("d@e.f",))


def test_stacked_parametrize_puts_the_outermost_first() -> None:
    @velox.parametrize("outer", [1, 2])
    @velox.parametrize("inner", ["a", "b"])
    def target(outer: int, inner: str) -> None: ...

    outer, inner = marks_of(target).parametrizations
    assert outer.argnames == ("outer",), "outermost first, so it varies slowest under product()"
    assert inner.argnames == ("inner",)


def test_parametrize_rejects_a_mismatched_case() -> None:
    with pytest.raises(ValueError, match="expected 2 value"):
        velox.parametrize("a,b", [(1, 2), (3,)])


def test_builtin_fixtures_are_fixtures() -> None:
    assert velox.tmp_path.scope == "function"
    assert velox.tmp_path_factory.scope == "session"
    assert all(
        isinstance(f, velox.Fixture)
        for f in (velox.tmp_path, velox.capture, velox.log_records, velox.test_info)
    )


def test_log_records_set_level_raises_at_call_time_not_at_enter() -> None:
    """`set_level` validates its `level`/`logger` arguments the moment it is called, not
    deferred until `with ...:` is entered."""
    records = velox.LogRecords([])

    with pytest.raises(ValueError, match="unknown logging level"):
        records.set_level("not-a-real-level")

    with pytest.raises(TypeError, match="bool is not a valid logging level"):
        records.set_level(True)

    with pytest.raises(TypeError, match="expected str or None"):
        records.set_level("DEBUG", logger=123)  # type: ignore[arg-type]

    # The happy path really is a context manager, entered only afterwards, and it restores the
    # root logger's previous level rather than leaving DEBUG in place.
    previous = logging.getLogger().level
    with records.set_level("DEBUG"):
        assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger().level == previous

    # The restore must also survive the *abnormal* exit path -- a raised level must not leak
    # past a test whose body raised.
    with pytest.raises(RuntimeError, match="boom"), records.set_level("DEBUG"):
        assert logging.getLogger().level == logging.DEBUG
        raise RuntimeError("boom")
    assert logging.getLogger().level == previous


def test_approx_compares_both_ways() -> None:
    assert 0.1 + 0.2 == velox.approx(0.3)  # noqa: SIM300 — both orders are the point
    assert velox.approx(0.3) == 0.1 + 0.2
    assert velox.approx(0.3) != 0.3001
    assert velox.approx(0.3, rel=0.01) == 0.3001


def test_raises_matches_and_exposes_the_exception() -> None:
    with velox.raises(ValueError, match="bad input") as caught:
        raise ValueError("bad input here")

    assert caught.type is ValueError
    assert str(caught.value) == "bad input here"


def test_raises_fails_when_nothing_is_raised() -> None:
    with pytest.raises(AssertionError, match="DID NOT RAISE ValueError"), velox.raises(ValueError):
        pass


def test_raises_lets_an_unexpected_exception_through() -> None:
    with pytest.raises(KeyError), velox.raises(ValueError):
        raise KeyError("other")


def test_raises_fails_when_the_message_does_not_match() -> None:
    with pytest.raises(AssertionError, match="does not match"), velox.raises(ValueError, match="x"):
        raise ValueError("something else")
