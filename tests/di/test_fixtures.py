"""Tests for voci._di.fixtures: injection scanning, cycle/scope validation, and `plan_for`."""

from __future__ import annotations

from typing import Annotated, cast

import pytest

import voci
from voci import Depends
from voci._di.fixtures import (
    DIError,
    Injection,
    Scope,
    _check_acyclic,
    _check_missing_injections,
    case_value_id,
    dedupe_case_ids,
    exclusive_tokens_of,
    expand_cases,
    plan_for,
)


@voci.fixture()
def alpha() -> int:
    return 1


def test_ordinary_annotated_types_are_left_alone() -> None:
    @voci.fixture()
    def fine(db: Annotated[int, "not a dependency"] = Depends(alpha)) -> int:
        return db

    (injection,) = fine.plan
    assert injection.param == "db"


def test_check_acyclic_detects_a_rigged_cycle() -> None:
    """A single `@voci.fixture()` decoration can never build a cycle — a dependency must
    already exist as an object before it can be depended on — so the detector is exercised
    directly against a graph rigged to loop."""
    a = voci.fixture()(lambda: 0)
    b = voci.fixture()(lambda: 0)
    a._plan = (Injection(param="b", source=b, keyword_only=False),)
    b._plan = (Injection(param="a", source=a, keyword_only=False),)

    with pytest.raises(ValueError, match="cycle"):
        _check_acyclic(a)


def test_check_acyclic_allows_a_diamond() -> None:
    """B and C both depending on D is not a cycle, just the same node reached twice."""
    d = voci.fixture()(lambda: 0)

    @voci.fixture()
    def b(x: int = Depends(d)) -> int:
        return x

    @voci.fixture()
    def c(x: int = Depends(d)) -> int:
        return x

    @voci.fixture()
    def top(p: int = Depends(b), q: int = Depends(c)) -> int:
        return p + q

    _check_acyclic(top)  # must not raise


# `plan_for`: step ordering, deduplication, and validation.
# ------------------------------------------------------------------------------------------


def test_plan_for_orders_steps_dependency_before_dependent() -> None:
    @voci.fixture()
    def c() -> str:
        return "c"

    @voci.fixture()
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

    @voci.fixture()
    def d() -> str:
        return "d"

    @voci.fixture()
    def b(x: str = Depends(d)) -> str:
        return x

    @voci.fixture()
    def c(x: str = Depends(d)) -> str:
        return x

    async def test_func(p: str = Depends(b), q: str = Depends(c)) -> None:
        pass

    plan = plan_for(test_func)

    names = [step.fixture.name for step in plan.steps]
    assert names.count("d") == 1
    assert len(plan.steps) == 3  # d, b, c — not four


def test_plan_for_never_deduplicates_call_scope_even_in_a_diamond() -> None:
    """A `scope="call"` fixture reached by two paths gets two independent steps, unlike every
    other scope."""

    @voci.fixture(scope="call")
    def d() -> object:
        return object()

    @voci.fixture()
    def b(x: object = Depends(d)) -> object:
        return x

    @voci.fixture()
    def c(x: object = Depends(d)) -> object:
        return x

    async def test_func(p: object = Depends(b), q: object = Depends(c)) -> None:
        pass

    plan = plan_for(test_func)

    names = [step.fixture.name for step in plan.steps]
    assert names.count("d") == 2
    assert len(plan.steps) == 4  # d, d, b, c


@pytest.mark.parametrize("wide_scope, narrow_scope", [("session", "function"), ("module", "call")])
def test_plan_for_rejects_a_wide_scope_fixture_depending_on_a_narrower_one(
    wide_scope: str, narrow_scope: str
) -> None:
    """Both offending fixture names appear in the error message."""

    @voci.fixture(scope=cast("Scope", narrow_scope), name="narrow_fx")
    def narrow() -> int:
        return 1

    @voci.fixture(scope=cast("Scope", wide_scope), name="wide_fx")
    def wide(x: int = Depends(narrow)) -> int:
        return x

    async def test_func(w: int = Depends(wide)) -> None:
        pass

    with pytest.raises(DIError) as excinfo:
        plan_for(test_func)

    message = str(excinfo.value)
    assert "narrow_fx" in message
    assert "wide_fx" in message


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


# `implicit=` -- the fixtures a container declares with `voci.use(...)`, which get a step each
# but bind to no parameter.
# ------------------------------------------------------------------------------------------


def test_implicit_fixtures_get_a_step_but_never_a_root_arg() -> None:
    @voci.fixture()
    def side_effect() -> None:
        return None

    async def test_func() -> None:
        pass

    plan = plan_for(test_func, implicit=[side_effect])

    assert [step.fixture.name for step in plan.steps] == ["side_effect"]
    assert plan.root_args == ()


def test_implicit_fixtures_are_built_before_the_tests_own_dependencies() -> None:
    """Lowest `step_id`s, so `_di.setup` constructs them first and `_di.teardown`, walking
    `steps` backwards, releases them last."""

    @voci.fixture()
    def declared() -> str:
        return "declared"

    @voci.fixture()
    def asked_for() -> str:
        return "asked_for"

    async def test_func(x: str = Depends(asked_for)) -> None:
        pass

    plan = plan_for(test_func, implicit=[declared])

    assert [step.fixture.name for step in plan.steps] == ["declared", "asked_for"]
    assert plan.root_args == (("x", 1, False),)


def test_implicit_fixtures_apply_in_the_order_they_were_declared() -> None:
    @voci.fixture()
    def first() -> int:
        return 1

    @voci.fixture()
    def second() -> int:
        return 2

    async def test_func() -> None:
        pass

    plan = plan_for(test_func, implicit=[first, second])

    assert [step.fixture.name for step in plan.steps] == ["first", "second"]


def test_a_fixture_both_declared_and_depended_on_is_built_once_and_still_bound() -> None:
    """The `plan_for` memo covers implicit roots too, so declaring a fixture a test also wants
    the value of doesn't construct it twice."""

    @voci.fixture()
    def shared() -> str:
        return "shared"

    async def test_func(x: str = Depends(shared)) -> None:
        pass

    plan = plan_for(test_func, implicit=[shared])

    assert [step.fixture.name for step in plan.steps] == ["shared"]
    assert plan.root_args == (("x", 0, False),)


def test_implicit_fixtures_pull_in_their_own_transitive_dependencies() -> None:
    @voci.fixture()
    def leaf() -> str:
        return "leaf"

    @voci.fixture()
    def declared(x: str = Depends(leaf)) -> str:
        return x

    async def test_func() -> None:
        pass

    plan = plan_for(test_func, implicit=[declared])

    assert [step.fixture.name for step in plan.steps] == ["leaf", "declared"]
    assert plan.steps[1].args == (("x", 0, False),)


def test_an_implicit_fixture_narrower_than_the_test_is_rejected() -> None:
    """Same scope-compatibility rule as a direct `Depends()` site, same error text."""

    @voci.fixture(scope="call", name="narrow_fx")
    def narrow() -> int:
        return 1

    @voci.fixture(scope="session", name="wide_fx")
    def wide(x: int = Depends(narrow)) -> int:
        return x

    async def test_func() -> None:
        pass

    with pytest.raises(DIError, match="wide_fx"):
        plan_for(test_func, implicit=[wide])


def test_an_implicit_fixtures_exclusive_token_reaches_the_admission_gate() -> None:
    @voci.fixture(exclusive="database")
    def declared() -> None:
        return None

    async def test_func() -> None:
        pass

    assert exclusive_tokens_of(plan_for(test_func, implicit=[declared])) == {"database"}


# `exclusive_tokens_of`: the resource-token set `_run.AdmissionGate` admits tests against.
# ------------------------------------------------------------------------------------------


def test_exclusive_tokens_of_is_empty_with_no_exclusive_fixtures() -> None:
    async def test_func(x: int = Depends(alpha)) -> None:
        pass

    assert exclusive_tokens_of(plan_for(test_func)) == frozenset()


def test_exclusive_tokens_of_collects_true_and_string_tokens() -> None:
    @voci.fixture(exclusive=True)
    def private() -> int:
        return 1

    @voci.fixture(exclusive="db")
    def shared() -> int:
        return 2

    async def test_func(a: int = Depends(private), b: int = Depends(shared)) -> None:
        pass

    assert exclusive_tokens_of(plan_for(test_func)) == {private, "db"}


def test_exclusive_true_is_a_token_private_to_its_own_fixture() -> None:
    """Two different `exclusive=True` fixtures never share a token -- only two tests depending on
    the very same fixture object do."""

    @voci.fixture(exclusive=True)
    def res_a() -> int:
        return 1

    @voci.fixture(exclusive=True)
    def res_b() -> int:
        return 2

    async def test_one(x: int = Depends(res_a)) -> None:
        pass

    async def test_two(x: int = Depends(res_b)) -> None:
        pass

    assert exclusive_tokens_of(plan_for(test_one)).isdisjoint(
        exclusive_tokens_of(plan_for(test_two))
    )


def test_exclusive_string_token_is_shared_across_different_fixtures() -> None:
    @voci.fixture(exclusive="db")
    def conn_a() -> int:
        return 1

    @voci.fixture(exclusive="db")
    def conn_b() -> int:
        return 2

    async def test_one(x: int = Depends(conn_a)) -> None:
        pass

    async def test_two(x: int = Depends(conn_b)) -> None:
        pass

    assert exclusive_tokens_of(plan_for(test_one)) == exclusive_tokens_of(plan_for(test_two))


def test_exclusive_tokens_of_reaches_a_transitive_dependency() -> None:
    @voci.fixture(exclusive="db")
    def db() -> int:
        return 1

    @voci.fixture()
    def wrapper(x: int = Depends(db)) -> int:
        return x

    async def test_func(y: int = Depends(wrapper)) -> None:
        pass

    assert exclusive_tokens_of(plan_for(test_func)) == {"db"}


def test_fixture_with_a_missing_injection_is_rejected_at_decoration_time() -> None:
    """`_check_missing_injections` runs the moment any `Fixture` is built (`Fixture.__init__`),
    so a required, non-injected parameter is caught at decoration time, before any test reaches
    it via `Depends(...)`."""
    with pytest.raises(DIError, match="conn"):

        @voci.fixture()
        def needs_arg(conn: object) -> object:
            return conn


def test_depends_on_a_positional_only_parameter_is_rejected_at_decoration_time() -> None:
    """`_di.setup`/`_construct` bind every injection by keyword; a positional-only parameter can
    never receive one, so this is caught statically instead of surfacing as an opaque `TypeError`
    at construction time."""
    with pytest.raises(DIError, match="x"):

        @voci.fixture()
        def outer(x: int = Depends(alpha), /) -> int:
            return x


# `_check_missing_injections` directly, one case per `co_varnames` slicing edge case.
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


# `known_params` -- the names `@voci.parametrize` supplies, threaded through from `plan_for`.
# ------------------------------------------------------------------------------------------


def test_known_params_are_not_reported_missing() -> None:
    def t(n: int, expected: int) -> None:
        pass

    _check_missing_injections(t, (), known_params=frozenset({"n", "expected"}))  # must not raise


def test_known_params_do_not_excuse_a_parameter_they_do_not_cover() -> None:
    def t(n: int, other: int) -> None:
        pass

    with pytest.raises(DIError, match="other"):
        _check_missing_injections(t, (), known_params=frozenset({"n"}))


def test_known_params_work_for_a_keyword_only_parameter_too() -> None:
    def t(*, n: int) -> None:
        pass

    _check_missing_injections(t, (), known_params=frozenset({"n"}))  # must not raise


def test_plan_for_expands_around_a_parametrize_name_alongside_depends() -> None:
    """The exact shape of the bug this closes: a parametrized extra parameter must not read as a
    missing `Depends(...)` injection, and a real injection alongside it still resolves."""

    async def test_func(n: int, value: int = Depends(alpha)) -> None:
        pass

    plan = plan_for(test_func, known_params=frozenset({"n"}))
    assert [step.fixture.name for step in plan.steps] == ["alpha"]
    assert plan.root_args == (("value", 0, False),)


def test_known_params_colliding_with_an_injected_name_is_rejected() -> None:
    async def test_func(value: int = Depends(alpha)) -> None:
        pass

    with pytest.raises(DIError, match="value"):
        plan_for(test_func, known_params=frozenset({"value"}))


def test_known_params_on_a_positional_only_parameter_is_rejected() -> None:
    def t(n: int, /) -> None:
        pass

    with pytest.raises(DIError, match="n"):
        _check_missing_injections(t, (), known_params=frozenset({"n"}))


def test_a_known_param_matching_no_real_parameter_is_rejected() -> None:
    """A typo'd `@voci.parametrize` argument name -- one that matches nothing in the
    signature -- is caught here instead of surfacing as a confusing runtime `TypeError` once
    expansion tries to call the function with it."""

    def t(n: int) -> None:
        pass

    with pytest.raises(DIError, match="typo"):
        _check_missing_injections(t, (), known_params=frozenset({"n", "typo"}))


def test_a_known_param_matching_no_parameter_is_allowed_with_star_kwargs() -> None:
    def t(**kwargs: object) -> None:
        pass

    _check_missing_injections(t, (), known_params=frozenset({"anything"}))  # must not raise


# `@voci.fixture(params=...)`: validation and id generation.
# ------------------------------------------------------------------------------------------


def test_fixture_params_default_ids_use_the_literal_for_common_types() -> None:
    @voci.fixture(params=["sqlite", "postgres"])
    def backend(param: str) -> str:
        return param

    assert backend.params == ("sqlite", "postgres")
    assert backend.case_ids == ("sqlite", "postgres")


def test_fixture_params_explicit_ids_are_used_verbatim() -> None:
    @voci.fixture(params=[1, 2], ids=["one", "two"])
    def n(param: int) -> int:
        return param

    assert n.case_ids == ("one", "two")


def test_fixture_params_ids_callable_falls_back_to_auto_id_on_none() -> None:
    @voci.fixture(params=[1, "x"], ids=lambda v: f"custom-{v}" if isinstance(v, int) else None)
    def mixed(param: object) -> object:
        return param

    assert mixed.case_ids == ("custom-1", "x")


def test_fixture_params_ids_are_deduped_on_collision() -> None:
    """Exercises the same cross-check `_collection.parametrize._dedupe` documents: a naive
    rename of the two colliding `1`s to `10`/`11` would collide with the third case's already-
    unique `"10"`, so both land past it instead."""

    @voci.fixture(params=[1, 1, "10"])
    def dup(param: object) -> object:
        return param

    assert dup.case_ids == ("11", "12", "10")


def test_fixture_with_no_params_is_not_parametrized() -> None:
    @voci.fixture()
    def plain() -> int:
        return 1

    assert plain.params == ()
    assert plain.case_ids == ()


def test_fixture_params_empty_is_rejected() -> None:
    with pytest.raises(ValueError, match="no values given"):

        @voci.fixture(params=())
        def bad(param: object) -> object:
            return param


def test_fixture_ids_without_params_is_rejected() -> None:
    with pytest.raises(ValueError, match="ids="):

        @voci.fixture(ids=["a"])
        def bad() -> int:
            return 1


def test_fixture_ids_length_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="id"):

        @voci.fixture(params=[1, 2], ids=["only-one"])
        def bad(param: int) -> int:
            return param


def test_fixture_missing_the_param_argument_is_rejected_at_decoration_time() -> None:
    with pytest.raises(DIError, match="param"):

        @voci.fixture(params=[1, 2])
        def bad(other: int) -> int:
            return other


def test_fixture_param_argument_cannot_also_be_depends_injected() -> None:
    with pytest.raises(DIError, match="param"):

        @voci.fixture(params=[1, 2])
        def bad(param: int = Depends(alpha)) -> int:
            return param


def test_case_value_id_and_dedupe_case_ids_match_parametrizes_own_rules() -> None:
    assert case_value_id(True, "param", 0) == "True"
    assert case_value_id({"a": 1}, "param", 3) == "param3"
    assert dedupe_case_ids(["x", "x", "y"]) == ("x0", "x1", "y")


# `plan_for`/`expand_cases`: fanning a test out across a parametrized fixture's cases.
# ------------------------------------------------------------------------------------------


def test_plan_for_records_no_param_ancestors_without_any_parametrized_fixture() -> None:
    async def test_func(x: int = Depends(alpha)) -> None:
        pass

    plan = plan_for(test_func)
    assert plan.param_ancestors == ()
    assert all(step.param_ancestors == () for step in plan.steps)


def test_plan_for_records_the_fixtures_own_id_as_its_ancestor() -> None:
    @voci.fixture(params=["a", "b"])
    def backend(param: str) -> str:
        return param

    async def test_func(x: str = Depends(backend)) -> None:
        pass

    plan = plan_for(test_func)
    assert plan.param_ancestors == (id(backend),)
    assert plan.steps[0].param_ancestors == (id(backend),)


def test_plan_for_propagates_param_ancestors_to_a_dependent_fixture() -> None:
    @voci.fixture(params=["a", "b"])
    def backend(param: str) -> str:
        return param

    @voci.fixture()
    def engine(b: str = Depends(backend)) -> str:
        return f"engine+{b}"

    async def test_func(x: str = Depends(engine)) -> None:
        pass

    plan = plan_for(test_func)
    steps_by_name = {step.fixture.name: step for step in plan.steps}
    assert steps_by_name["backend"].param_ancestors == (id(backend),)
    assert steps_by_name["engine"].param_ancestors == (id(backend),)
    assert plan.param_ancestors == (id(backend),)


def test_plan_for_records_param_ancestors_reached_only_through_an_implicit_fixture() -> None:
    """A `params=` fixture a container declared still fans its tests out one case each, so it
    has to reach `ResolutionPlan.param_ancestors` the same way a directly-depended one does."""

    @voci.fixture(params=["a", "b"])
    def backend(param: str) -> str:
        return param

    async def test_func() -> None:
        pass

    plan = plan_for(test_func, implicit=[backend])

    assert plan.param_ancestors == (id(backend),)
    assert [e.plan.steps[0].param_value for e in expand_cases(plan)] == ["a", "b"]


def test_expand_cases_passes_through_the_same_plan_object_when_unparametrized() -> None:
    async def test_func(x: int = Depends(alpha)) -> None:
        pass

    plan = plan_for(test_func)
    (expansion,) = expand_cases(plan)
    assert expansion.plan is plan
    assert expansion.case_id is None


def test_expand_cases_produces_one_plan_per_case() -> None:
    @voci.fixture(params=["sqlite", "postgres"])
    def backend(param: str) -> str:
        return param

    async def test_func(x: str = Depends(backend)) -> None:
        pass

    plan = plan_for(test_func)
    expansions = expand_cases(plan)

    assert [e.case_id for e in expansions] == ["sqlite", "postgres"]
    assert [e.plan.steps[0].param_value for e in expansions] == ["sqlite", "postgres"]
    # Distinct plan objects, with distinct cache-key material per case.
    assert expansions[0].plan is not expansions[1].plan
    assert expansions[0].plan.steps[0].case_key != expansions[1].plan.steps[0].case_key


def test_expand_cases_propagates_the_chosen_case_key_to_a_dependent_step() -> None:
    @voci.fixture(params=["a", "b"])
    def backend(param: str) -> str:
        return param

    @voci.fixture()
    def engine(b: str = Depends(backend)) -> str:
        return f"engine+{b}"

    async def test_func(x: str = Depends(engine)) -> None:
        pass

    plan = plan_for(test_func)
    expansions = expand_cases(plan)

    for expansion in expansions:
        backend_step = next(s for s in expansion.plan.steps if s.fixture is backend)
        engine_step = next(s for s in expansion.plan.steps if s.fixture is engine)
        assert engine_step.case_key == backend_step.case_key


def test_expand_cases_cross_multiplies_two_independent_parametrized_fixtures() -> None:
    @voci.fixture(params=["a", "b"])
    def left(param: str) -> str:
        return param

    @voci.fixture(params=[1, 2])
    def right(param: int) -> int:
        return param

    async def test_func(x: str = Depends(left), y: int = Depends(right)) -> None:
        pass

    plan = plan_for(test_func)
    expansions = expand_cases(plan)

    assert [e.case_id for e in expansions] == ["a-1", "a-2", "b-1", "b-2"]


def test_expand_cases_case_key_only_reflects_a_steps_actual_ancestors() -> None:
    """A step depending on only one of two independent parametrized fixtures must not fragment
    its cache key over the other one's cases too."""

    @voci.fixture(params=["a", "b"])
    def left(param: str) -> str:
        return param

    @voci.fixture(params=[1, 2])
    def right(param: int) -> int:
        return param

    @voci.fixture()
    def left_only(x: str = Depends(left)) -> str:
        return x

    async def test_func(a: str = Depends(left_only), b: int = Depends(right)) -> None:
        pass

    plan = plan_for(test_func)
    expansions = expand_cases(plan)

    left_only_case_keys = {
        next(s for s in expansion.plan.steps if s.fixture is left_only).case_key
        for expansion in expansions
    }
    # Only two distinct case_keys for `left_only` across all four combos -- one per `left` case,
    # not one per combo -- since it never depends on `right`.
    assert len(left_only_case_keys) == 2
