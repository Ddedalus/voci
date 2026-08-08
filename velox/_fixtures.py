"""Fixture objects, the `Depends` sentinel, and the static injection/resolution plans.

This module owns the objects the rest of velox is built on: `Fixture`, which is what
`@velox.fixture()` returns, the sentinel `Depends()` leaves in `__defaults__`, and — the M1
addition — `ResolutionPlan`, the flattened, statically-validated, topologically-sorted
construction order for one test function's whole transitive fixture graph (spec/04 §1-2).

Both plans are built once, at decoration/collection time, by scanning `__defaults__` /
`__kwdefaults__` — never `inspect.signature`, never `get_type_hints`. Annotations are for the
reader and the type checker only (spec/01 design rule 3).

`ResolutionPlan` is a flat list, not a tree: `_di.py` executes it as a straight loop with no
graph walking at run time (spec/04 §1, I5). Building it *is* the graph walk, done exactly once
per test function, memoized so a diamond-shaped graph produces one step per fixture rather than
one per path to it — except `scope="call"` fixtures, which are deliberately never memoized (spec
/04's "never shared" is a statement about the *plan*, not just the runtime cache: every
`Depends()` site pointing at a call-scoped fixture gets its own step and, later, its own cache
key). Actual construction, caching, and teardown are `_di.py`'s job; this module only decides
*what* needs building and *in what order* — the declarative half.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, cast, final, get_args, get_origin, overload

__all__ = [
    "DIError",
    "Depends",
    "Fixture",
    "Injection",
    "PlanStep",
    "ResolutionPlan",
    "Scope",
    "fixture",
    "plan_for",
    "plan_of",
]

type Scope = Literal["call", "function", "module", "session"]
"""How widely one constructed instance is shared.

- `"call"` — never shared. A fresh instance per `Depends(...)` site, so a test asking for the
  same fixture twice gets two distinct values. Teardown still runs at end of test.
- `"function"` — one instance per test (the default).
- `"module"` — one instance per test module.
- `"session"` — one instance per run.
"""

type Exclusive = bool | str
"""`True` for a private token, or a shared string token naming the contended resource."""


@final
@dataclass(frozen=True, slots=True)
class Injection:
    """One `Depends(...)` site on a fixture or test callable."""

    param: str
    source: Fixture[Any]
    keyword_only: bool


@final
@dataclass(frozen=True, slots=True)
class Dependency[T]:
    """The run-time sentinel `Depends()` returns. Never seen by user code as itself."""

    fixture: Fixture[T]


def Depends[T](dependency: Fixture[T], /) -> T:
    """Declare an injected parameter: `param: Type = Depends(some_fixture)`.

    Takes the `Fixture` object, not a name and not a bare callable. Typed as returning `T` so the
    annotation on the parameter is genuinely checked; returns a sentinel at run time.

    The `isinstance` check is spec/04 §2's "`Depends()` on a non-fixture" validation, caught here
    rather than deferred to plan-building: `Depends()` is called at class-body/module-body
    evaluation time (it's sitting in a default), which is strictly earlier than collection, so
    catching it here gives the earliest possible, most-precisely-attributed diagnostic — the
    traceback points at the actual `Depends(...)` call site instead of an opaque failure three
    layers into `plan_for`.
    """
    if not isinstance(dependency, Fixture):
        raise TypeError(
            f"Depends() requires a Fixture object, built with @velox.fixture() -- got "
            f"{dependency!r}"
        )
    return cast(T, Dependency(dependency))


def plan_of(func: Callable[..., Any]) -> tuple[Injection, ...]:
    """Read the injection plan off a callable's defaults.

    A parameter is injected iff its default is a `Depends(...)` sentinel. Any other default is an
    ordinary Python default and is left alone — that is what keeps `@parametrize` and plain
    closures working.
    """
    code = getattr(func, "__code__", None)
    if code is None:
        return ()

    _reject_annotated_depends(func)

    injections: list[Injection] = []

    positional = code.co_varnames[: code.co_argcount]
    defaults = func.__defaults__ or ()
    offset = len(positional) - len(defaults)
    for param, default in zip(positional[offset:], defaults, strict=True):
        if (injection := _injection(param, default, keyword_only=False)) is not None:
            injections.append(injection)

    for param, default in (func.__kwdefaults__ or {}).items():
        if (injection := _injection(param, default, keyword_only=True)) is not None:
            injections.append(injection)

    return tuple(injections)


def _reject_annotated_depends(func: Callable[..., Any]) -> None:
    """Catch `Depends(...)` written inside `Annotated[...]` metadata instead of default position.

    `def t(db: Annotated[Session, Depends(db_fx)])` is the FastAPI spelling, and it is a habit
    users arrive with — it reads as injected, but since `plan_of` only ever looks at
    `__defaults__`/`__kwdefaults__` (spec/01 rule 3, deliberately never `inspect.signature` or
    `get_type_hints`), the parameter gets nothing and the test would run with a raw `Dependency`
    object bound to `db`. Nothing downstream would ever catch that, so it is caught here instead,
    at decoration time, pointing at the working spelling.

    Best effort only: this reads raw `__annotations__` purely to detect the mistake, not to build
    the plan, so a string annotation (postponed evaluation) or any other exotic annotation is
    silently left alone rather than risking a spurious raise.
    """
    annotations = getattr(func, "__annotations__", None)
    if not annotations:
        return
    for param, annotation in annotations.items():
        try:
            if get_origin(annotation) is not Annotated:
                continue
            stray = any(isinstance(m, Dependency) for m in get_args(annotation)[1:])
        except Exception:  # diagnostics only; never let an odd annotation raise
            continue
        if stray:
            name = getattr(func, "__name__", repr(func))
            raise TypeError(
                f"{name}({param!r}): Depends(...) found inside Annotated[...] metadata, which "
                f"velox never reads. Use default position instead: `{param}: ... = Depends(...)`."
            )


def _injection(param: str, default: object, *, keyword_only: bool) -> Injection | None:
    if not isinstance(default, Dependency):
        return None
    return Injection(param=param, source=default.fixture, keyword_only=keyword_only)


@final
class Fixture[T]:
    """A fixture: the callable, plus everything the scheduler needs to know statically.

    Constructed by `@velox.fixture()`. Immutable — there is no method that derives a modified
    copy. Per-node override (replacing one named dependency of an existing `Fixture` to get a new
    one) is roadmap; see spec/01 §10 for why it was deferred rather than shipped.
    """

    __slots__ = ("_exclusive", "_func", "_name", "_plan", "_scope")

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        scope: Scope = "function",
        exclusive: Exclusive = False,
        name: str | None = None,
    ) -> None:
        self._func = func
        self._scope: Scope = scope
        self._exclusive: Exclusive = exclusive
        self._name = name if name is not None else getattr(func, "__name__", repr(func))
        self._plan = plan_of(func)
        _check_acyclic(self)

    @property
    def func(self) -> Callable[..., Any]:
        """The undecorated callable. Sync, async, sync generator, or async generator."""
        return self._func

    @property
    def name(self) -> str:
        """Display name in errors, reports, and `--durations`."""
        return self._name

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def exclusive(self) -> Exclusive:
        return self._exclusive

    @property
    def plan(self) -> tuple[Injection, ...]:
        """This fixture's own `Depends(...)` sites."""
        return self._plan

    @property
    def dependencies(self) -> tuple[Fixture[Any], ...]:
        """The fixture nodes this one depends on, for graph walking.

        Acyclic by construction — `Fixture.__init__` checks the moment a fixture is built, so
        every walker downstream (the scheduler, a future `--graph` dump) can recurse over this
        without a visited-set of its own.
        """
        return tuple(i.source for i in self._plan)

    def __call__(self, *args: object, **kwargs: object) -> Any:
        """Call the underlying function directly, as ordinary Python.

        No injection is performed — any un-passed `Depends(...)` parameter keeps its sentinel
        default. This exists so a fixture stays a normal callable; overriding one dependency for
        one test is done by writing a separate fixture function with the replacement wired in and
        passing *that* to `Depends(...)` at the call site (tier (a), spec/08 §3) — replacing one
        named dependency of an existing `Fixture` in place is roadmap (spec/01 §10).
        """
        return self._func(*args, **kwargs)

    def __repr__(self) -> str:
        exclusive = "" if self._exclusive is False else f" exclusive={self._exclusive!r}"
        return f"<Fixture {self._name} scope={self._scope}{exclusive}>"


def _check_acyclic(root: Fixture[Any]) -> None:
    """Raise if `root`'s dependency graph loops back on itself.

    A single `@velox.fixture()` decoration can never produce a cycle — to depend on a fixture it
    has to already exist as an object — but a future late-rebind mechanism (deep per-node
    override, spec/01 §10, is the leading candidate) could, and the check belongs here once
    rather than in every future graph walker. Identity-keyed (`id()`), not `Fixture.__eq__`/
    `__hash__`: giving `Fixture` structural equality is exactly the open question that deferred
    that feature, and this must not force that decision.
    """
    on_path: set[int] = set()

    def walk(node: Fixture[Any]) -> None:
        node_id = id(node)
        if node_id in on_path:
            raise ValueError(f"dependency cycle detected at fixture {node.name!r}")
        on_path.add(node_id)
        for dep in node.dependencies:
            walk(dep)
        on_path.discard(node_id)

    walk(root)


class FixtureDecorator(Protocol):
    """What `velox.fixture(...)` returns: a decorator that unwraps the yielded/awaited type."""

    @overload
    def __call__[T](self, fn: Callable[..., AsyncIterator[T]], /) -> Fixture[T]: ...
    @overload
    def __call__[T](self, fn: Callable[..., Iterator[T]], /) -> Fixture[T]: ...
    @overload
    def __call__[T](self, fn: Callable[..., Awaitable[T]], /) -> Fixture[T]: ...
    @overload
    def __call__[T](self, fn: Callable[..., T], /) -> Fixture[T]: ...


def fixture(
    *,
    scope: Scope = "function",
    exclusive: Exclusive = False,
    name: str | None = None,
) -> FixtureDecorator:
    """Declare a fixture. Parentheses are always required, even with no arguments.

    :param scope: how widely one constructed instance is shared. See `Scope`.
    :param exclusive: `True`, or a string token naming a contended resource. Tests transitively
        depending on it never run concurrently with each other (spec/06).
    :param name: display name in errors, reports, and `--durations`. Defaults to `fn.__name__`.

    There is no `autouse` — a dependency you cannot see in the signature is exactly what velox
    exists to remove. There is no `params` / `ids` yet; parametrized fixtures are roadmap.
    """

    def decorate(fn: Callable[..., Any], /) -> Fixture[Any]:
        return Fixture(fn, scope=scope, exclusive=exclusive, name=name)

    return cast(FixtureDecorator, decorate)


# --------------------------------------------------------------------------------------------
# Resolution plans (spec/04 §1-2) — M1.
# --------------------------------------------------------------------------------------------


class DIError(Exception):
    """A static DI validation failure: scope nesting, a missing injection.

    Raised by `plan_for`, always before any test runs. `_collect.collect` catches this and turns
    it into a `CollectionError` attributed to the offending test, alongside every other kind of
    collection failure (spec/04 §2's "exits `4` with all errors listed rather than the first" is
    the run-wide version of that same idea; M1 keeps the per-file `CollectionError` list rather
    than introducing a second severity tier — see `_collect.py`).
    """


#: SESSION > MODULE > FUNCTION; a fixture may depend only on equal-or-wider scopes (spec/04 §2).
#: `"call"` is ranked with `"function"`: both tear down at end of test (spec/04 §4's teardown
#: column), and the compatibility rule only cares about *lifetime*, not the caching behavior that
#: otherwise tells the two apart. A `Fixture.scope` is always one of these four keys.
_SCOPE_RANK: dict[Scope, int] = {"session": 3, "module": 2, "function": 1, "call": 1}


@final
@dataclass(frozen=True, slots=True)
class PlanStep:
    """One fixture construction, in an already-topologically-sorted `ResolutionPlan.steps`.

    `args` names this fixture's own `Depends(...)` parameters, each resolved to the `step_id` of
    an *earlier* step in the same plan that supplies it — never a `Fixture` reference, so
    executing a plan is array indexing, not graph walking (spec/04 §1, I5).
    """

    step_id: int
    fixture: Fixture[Any]
    args: tuple[tuple[str, int, bool], ...]
    """`(param, source step_id, keyword_only)` per `Depends(...)` site on this fixture."""


@final
@dataclass(frozen=True, slots=True)
class ResolutionPlan:
    """The whole transitive fixture graph one test function needs, flattened and ordered.

    `steps` is dependency-before-dependent (a valid topological order of the graph) — `_di.py`
    constructs by walking it forwards and tears down by walking it *backwards*, which is what
    gives teardown inversion (dependents unwind before dependencies, spec/04 §5) without any
    extra bookkeeping: reversing a topological order is always a valid reverse-topological order.

    A fixture reachable by more than one path (a diamond) gets exactly one step — see the module
    docstring for why `scope="call"` fixtures are the deliberate exception.
    """

    steps: tuple[PlanStep, ...]
    root_args: tuple[tuple[str, int, bool], ...]
    """The test function's own `Depends(...)` sites, resolved the same way as `PlanStep.args`."""


def plan_for(func: Callable[..., Any]) -> ResolutionPlan:
    """Build `func`'s `ResolutionPlan`: a topological walk over its `Depends(...)` graph.

    `func` is a test function (an already-decorated `Fixture` never needs this — its own
    dependencies are just `Fixture.dependencies`/`Fixture.plan`, walked fresh at the point
    something depends on *it*). Raises `DIError` for every static check spec/04 §2 lists that
    the graph shape alone can decide: scope compatibility and missing injections. Cycles and
    `Depends()`-on-a-non-`Fixture` are caught earlier and don't need re-checking here — cycles at
    `Fixture.__init__` (`_check_acyclic`, since a fixture can only ever depend on
    already-constructed `Fixture` objects, roadmap deep-override schemes aside) and a bad
    `Depends()` argument at `Depends()`'s own call site.
    """
    root_injections = plan_of(func)
    _check_missing_injections(func, root_injections)

    steps: list[PlanStep] = []
    #: `id(fixture) -> step_id`, non-`"call"` scopes only — the memo that turns a diamond into
    #: one step (see module docstring). Deliberately keyed by `id()`, not the `Fixture` itself:
    #: same rationale as `_check_acyclic`, and structural equality on `Fixture` is an open
    #: question this must not have an opinion on.
    memo: dict[int, int] = {}

    def visit(source: Fixture[Any], dependent: Fixture[Any] | None) -> int:
        """Ensure `source` has a step, return its `step_id`. `dependent` is `None` for the test
        function's own direct dependencies — used only for the scope check and error text."""
        dependent_scope: Scope = "function" if dependent is None else dependent.scope
        if _SCOPE_RANK[source.scope] < _SCOPE_RANK[dependent_scope]:
            holder = "the test itself" if dependent is None else f"fixture {dependent.name!r}"
            raise DIError(
                f"{holder} (scope={dependent_scope!r}) depends on fixture {source.name!r} "
                f"(scope={source.scope!r}): a narrower-scoped fixture would be torn down while "
                f"the wider one still held it (spec/04 §2)."
            )
        if source.scope != "call":
            cached = memo.get(id(source))
            if cached is not None:
                return cached
        args = tuple(
            (injection.param, visit(injection.source, source), injection.keyword_only)
            for injection in source.plan
        )
        step_id = len(steps)
        steps.append(PlanStep(step_id=step_id, fixture=source, args=args))
        if source.scope != "call":
            memo[id(source)] = step_id
        return step_id

    root_args = tuple(
        (injection.param, visit(injection.source, None), injection.keyword_only)
        for injection in root_injections
    )
    return ResolutionPlan(steps=tuple(steps), root_args=root_args)


def _check_missing_injections(func: Callable[..., Any], injections: tuple[Injection, ...]) -> None:
    """Raise `DIError` naming any parameter with no default that isn't `self` and isn't injected.

    Deliberately reads `__code__`/`__defaults__` rather than `inspect.signature` (module
    docstring, spec/01 rule 3) — this is a diagnostic, not the plan itself, but the same
    "annotations are never load-bearing" rule applies to keep the two code paths consistent.
    """
    code = getattr(func, "__code__", None)
    if code is None:
        return
    injected = {injection.param for injection in injections}

    positional = code.co_varnames[: code.co_argcount]
    n_defaulted = len(func.__defaults__ or ())
    required_positional = positional[: len(positional) - n_defaulted]
    missing = [p for p in required_positional if p not in injected and p != "self"]

    kwonly = code.co_varnames[code.co_argcount : code.co_argcount + code.co_kwonlyargcount]
    kwdefaults = func.__kwdefaults__ or {}
    missing += [p for p in kwonly if p not in kwdefaults and p not in injected]

    if missing:
        name = getattr(func, "__name__", repr(func))
        params = ", ".join(missing)
        raise DIError(
            f"{name}: parameter(s) {params} have no default and are not injected via "
            f"Depends(...) -- velox has no name-based fixture lookup, so an uninjected "
            f"parameter can never be supplied (spec/04 §2)."
        )
