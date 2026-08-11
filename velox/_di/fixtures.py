"""Fixture objects, the `Depends` sentinel, and the static injection/resolution plans.

This module owns the objects the rest of velox is built on: `Fixture`, what `@velox.fixture()`
returns, the sentinel `Depends()` leaves in `__defaults__`, and `ResolutionPlan`, the flattened,
statically-validated, topologically-sorted construction order for one test function's whole
transitive fixture graph.

Both are built once, at decoration/collection time, by scanning `__defaults__`/`__kwdefaults__` —
never `inspect.signature`, never `get_type_hints`. Annotations are for the reader and the type
checker only; they are never load-bearing.

`ResolutionPlan` is a flat list, not a tree: `_di.py` executes it as a straight loop with no graph
walking at run time. Building it *is* the graph walk, done once per test function and memoized so
a diamond-shaped graph produces one step per fixture rather than one per path to it — except
`scope="call"` fixtures, which are never memoized, since each `Depends()` site pointing at one
needs its own step and its own cache key. Construction, caching, and teardown are `_di.py`'s job;
this module only decides *what* needs building and *in what order* — the declarative half of the
DI system, with `_di.py` as the dynamic half.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, cast, final, get_args, get_origin, overload

__all__ = [
    "BuiltinContext",
    "BuiltinProvider",
    "DIError",
    "Depends",
    "Fixture",
    "Injection",
    "PlanStep",
    "ResolutionPlan",
    "Scope",
    "builtin_fixture",
    "fixture",
    "plan_for",
    "plan_of",
]

type Closer = Callable[[], Awaitable[None]]
"""Mirrors `_di.Closer`. Nothing enforces that the two definitions match beyond this comment."""

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

    Takes the `Fixture` object itself, not a name or a bare callable. Typed as returning `T` so
    the parameter's own annotation is checked normally, though at run time it returns a
    sentinel. Raises `TypeError` immediately if `dependency` isn't a `Fixture`.
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
    # `_di` binds every injection by keyword (`**kwargs`, never positionally), so a
    # `Depends(...)` default on a positional-only parameter (before `/`) could never actually be
    # supplied at construction time. Rejected here, at decoration time, rather than left to fail
    # later as an opaque `TypeError` naming no fixture.
    posonly_count = code.co_posonlyargcount
    for index, (param, default) in enumerate(
        zip(positional[offset:], defaults, strict=True), start=offset
    ):
        if (injection := _injection(param, default, keyword_only=False)) is not None:
            if index < posonly_count:
                name = getattr(func, "__name__", repr(func))
                raise DIError(
                    f"{name}({param!r}): Depends(...) on a positional-only parameter (before "
                    f"'/') is not supported -- velox binds every injection by keyword, and a "
                    f"positional-only parameter can never accept one. Move {param!r} after the "
                    f"'/', or stop injecting it."
                )
            injections.append(injection)

    for param, default in (func.__kwdefaults__ or {}).items():
        if (injection := _injection(param, default, keyword_only=True)) is not None:
            injections.append(injection)

    return tuple(injections)


def _reject_annotated_depends(func: Callable[..., Any]) -> None:
    """Catch `Depends(...)` written inside `Annotated[...]` metadata instead of default position.

    `plan_of` only reads `__defaults__`/`__kwdefaults__`, so `Depends()` inside
    `Annotated[...]` (the FastAPI spelling, e.g. `db: Annotated[Session, Depends(db_fx)]`) is
    silently ignored there; this raises `TypeError` for it instead. Best effort: a string
    annotation or other exotic form is left alone rather than risking a false positive.
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
@dataclass(frozen=True, slots=True)
class BuiltinContext:
    """The per-setup-call context a `BuiltinProvider` needs beyond its own injected `kwargs`:
    everything `_di.setup` already has to hand from its own parameters.

    A provider reaches other per-test facts — the capture sink, the log-record buffer, the
    concurrency-slot index, marks, the timeout budget — through `ContextVar`s set around each
    test's setup/call/teardown envelope.
    """

    test_id: str
    module_path: str


type BuiltinProvider = Callable[
    [Mapping[str, Any], BuiltinContext], Awaitable[tuple[Any, Closer | None]]
]
"""What a runtime-supplied (as opposed to user-written) fixture hands `_di._construct` instead of
a call to `Fixture.func` — the same `(value, closer)` shape `_construct` already produces for
every other fixture kind, so nothing downstream of construction (caching, refcounting, teardown)
needs to know the difference. See `builtin_fixture` below."""


@final
class Fixture[T]:
    """A fixture: the callable, plus everything the scheduler needs to know statically.

    Constructed by `@velox.fixture()`. Immutable.
    """

    __slots__ = ("_exclusive", "_func", "_name", "_plan", "_provider", "_scope")

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        scope: Scope = "function",
        exclusive: Exclusive = False,
        name: str | None = None,
        provider: BuiltinProvider | None = None,
    ) -> None:
        self._func = func
        self._scope: Scope = scope
        self._exclusive: Exclusive = exclusive
        self._name = name if name is not None else getattr(func, "__name__", repr(func))
        self._plan = plan_of(func)
        self._provider = provider
        # Checked here rather than only when some test's `plan_for` walk reaches this fixture: a
        # fixture can only ever be called with the parameters `Depends(...)` supplies (there is no
        # name-based lookup to fall back on), so a required, non-injected parameter is broken by
        # construction regardless of who depends on it. Checking at build time means it fires once
        # per fixture, not once per path a diamond graph reaches it by, and fires even for a
        # fixture no collected test currently uses.
        _check_missing_injections(func, self._plan)
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
    def provider(self) -> BuiltinProvider | None:
        """`None` for every ordinary fixture. When set, `_di._construct` calls this instead of
        `func` — see `builtin_fixture`. Not settable via `velox.fixture()`; only this package's
        own `_builtins.py` ever constructs a provider-backed `Fixture`."""
        return self._provider

    @property
    def plan(self) -> tuple[Injection, ...]:
        """This fixture's own `Depends(...)` sites."""
        return self._plan

    @property
    def dependencies(self) -> tuple[Fixture[Any], ...]:
        """The fixture nodes this one depends on, for graph walking.

        Acyclic by construction: `Fixture.__init__` checks at build time, so a walker can
        recurse over this without tracking a visited set.
        """
        return tuple(i.source for i in self._plan)

    def __call__(self, *args: object, **kwargs: object) -> Any:
        """Call the underlying function directly, as ordinary Python.

        No injection is performed: any un-passed `Depends(...)` parameter keeps its sentinel
        default. Overriding one dependency for one test means writing a separate fixture with
        the replacement wired in and passing that to `Depends(...)` at the call site.
        """
        return self._func(*args, **kwargs)

    def __repr__(self) -> str:
        exclusive = "" if self._exclusive is False else f" exclusive={self._exclusive!r}"
        return f"<Fixture {self._name} scope={self._scope}{exclusive}>"


def _check_acyclic(root: Fixture[Any]) -> None:
    """Raise if `root`'s dependency graph loops back on itself.

    Keyed by `id()`: `Fixture` has no structural equality, so identity is the only available
    notion of "same fixture".
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
        depending on it never run concurrently with each other.
    :param name: display name in errors, reports, and `--durations`. Defaults to `fn.__name__`.
    """

    def decorate(fn: Callable[..., Any], /) -> Fixture[Any]:
        return Fixture(fn, scope=scope, exclusive=exclusive, name=name)

    return cast(FixtureDecorator, decorate)


def builtin_fixture(
    func: Callable[..., Any],
    *,
    provider: BuiltinProvider,
    scope: Scope = "function",
    name: str | None = None,
) -> Fixture[Any]:
    """Construct a `Fixture` whose value the velox runtime supplies directly, instead of by
    calling `func`. `func` is kept for its `__name__`, signature and return annotation, which
    display and typecheck call sites against; `_di._construct` checks `.provider` first, so
    `func`'s own body never runs. Not exposed through `fixture()`; only `_builtins.py` calls
    this.
    """
    return Fixture(func, scope=scope, name=name, provider=provider)


class DIError(Exception):
    """A static DI validation failure: scope nesting, a missing injection.

    Raised by `plan_for`, always before any test runs. `_collect.collect` catches this and turns
    it into a `CollectionError` attributed to the offending test, alongside every other kind of
    collection failure.
    """


#: SESSION > MODULE > FUNCTION; a fixture may depend only on equal-or-wider scopes. `"call"` is
#: ranked with `"function"`: both tear down at end of test, and the compatibility rule only cares
#: about *lifetime*, not the caching behavior that otherwise tells the two apart.
_SCOPE_RANK: dict[Scope, int] = {"session": 3, "module": 2, "function": 1, "call": 1}


@final
@dataclass(frozen=True, slots=True)
class PlanStep:
    """One fixture construction, in an already-topologically-sorted `ResolutionPlan.steps`.

    `args` names this fixture's own `Depends(...)` parameters, each resolved to the `step_id` of
    an earlier step in the same plan — executing a plan is array indexing, not graph walking.
    """

    step_id: int
    fixture: Fixture[Any]
    args: tuple[tuple[str, int, bool], ...]
    """`(param, source step_id, keyword_only)` per `Depends(...)` site on this fixture."""


@final
@dataclass(frozen=True, slots=True)
class ResolutionPlan:
    """The whole transitive fixture graph one test function needs, flattened and ordered.

    `steps` is dependency-before-dependent, a valid topological order of the graph; `_di.py`
    constructs by walking it forwards and tears down by walking it backwards, producing
    dependent-before-dependency teardown order.

    A fixture reachable by more than one path (a diamond) gets exactly one step, except
    `scope="call"` fixtures, each of which gets its own.
    """

    steps: tuple[PlanStep, ...]
    root_args: tuple[tuple[str, int, bool], ...]
    """The test function's own `Depends(...)` sites, resolved the same way as `PlanStep.args`."""


def plan_for(func: Callable[..., Any]) -> ResolutionPlan:
    """Build `func`'s `ResolutionPlan`: a topological walk over its `Depends(...)` graph.

    `func` is a test function; an already-decorated `Fixture` never needs this, since its
    dependencies are already `Fixture.dependencies`/`Fixture.plan`. Raises `DIError` for scope
    compatibility and missing injections — the two checks the graph shape alone can decide.
    Cycles and `Depends()` on a non-`Fixture` are caught earlier, at `Fixture.__init__` and at
    `Depends()`'s own call site.
    """
    root_injections = plan_of(func)
    # Only the test function's own missing-injection check happens here; every fixture `visit`
    # below reaches already had its own body checked at `Fixture.__init__` time, independent of
    # who depends on it.
    _check_missing_injections(func, root_injections)

    steps: list[PlanStep] = []
    #: `id(fixture) -> step_id`, non-`"call"` scopes only — the memo that turns a diamond into
    #: one step. Keyed by `id()`, not the `Fixture` itself: `Fixture` has no structural equality.
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
                f"the wider one still held it."
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

    Reads `__code__`/`__defaults__`, the same way `plan_of` does, keeping the two code paths
    consistent.
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
            f"parameter can never be supplied."
        )
