"""Fixture objects, the `Depends` sentinel, and the static injection plan.

This module owns the two objects the rest of velox is built on: `Fixture`, which is what
`@velox.fixture()` returns, and the sentinel `Depends()` leaves in `__defaults__`.

The plan is built once, at decoration time, by scanning `__defaults__` / `__kwdefaults__` —
never `inspect.signature`, never `get_type_hints`. Annotations are for the reader and the type
checker only (spec/01 design rule 3).

Resolution, caching, and teardown are the runtime's job (spec/04, spec/05) and are not
implemented here; this module is the declarative half.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, cast, final, get_args, get_origin, overload

__all__ = [
    "Constant",
    "Depends",
    "Fixture",
    "Injection",
    "Scope",
    "fixture",
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
class Constant:
    """A plain value substituted for a dependency by `Fixture.with_`.

    Kept as a distinct node type so the static graph can still be walked: an overridden edge is
    visible as an edge, it just terminates immediately.
    """

    value: object


@final
@dataclass(frozen=True, slots=True)
class Injection:
    """One `Depends(...)` site on a fixture or test callable."""

    param: str
    source: Fixture[Any] | Constant
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
    """
    return cast(T, Dependency(dependency))


def plan_of(
    func: Callable[..., Any], overrides: Mapping[str, object] | None = None
) -> tuple[Injection, ...]:
    """Read the injection plan off a callable's defaults.

    A parameter is injected iff its default is a `Depends(...)` sentinel. Any other default is an
    ordinary Python default and is left alone — that is what keeps `@parametrize` and plain
    closures working.
    """
    code = getattr(func, "__code__", None)
    if code is None:
        return ()

    _reject_annotated_depends(func)

    overrides = overrides or {}
    injections: list[Injection] = []

    positional = code.co_varnames[: code.co_argcount]
    defaults = func.__defaults__ or ()
    offset = len(positional) - len(defaults)
    for param, default in zip(positional[offset:], defaults, strict=True):
        if (injection := _injection(param, default, overrides, keyword_only=False)) is not None:
            injections.append(injection)

    for param, default in (func.__kwdefaults__ or {}).items():
        if (injection := _injection(param, default, overrides, keyword_only=True)) is not None:
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


def _injection(
    param: str, default: object, overrides: Mapping[str, object], *, keyword_only: bool
) -> Injection | None:
    if not isinstance(default, Dependency):
        return None
    source = overrides.get(param, default.fixture)
    if not isinstance(source, Fixture):
        source = Constant(source)
    return Injection(param=param, source=source, keyword_only=keyword_only)


@final
class Fixture[T]:
    """A fixture: the callable, plus everything the scheduler needs to know statically.

    Constructed by `@velox.fixture()`. Immutable; `with_()` derives a new one.
    """

    __slots__ = ("_exclusive", "_func", "_name", "_overrides", "_plan", "_scope")

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        scope: Scope = "function",
        exclusive: Exclusive = False,
        name: str | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> None:
        self._func = func
        self._scope: Scope = scope
        self._exclusive: Exclusive = exclusive
        self._name = name if name is not None else getattr(func, "__name__", repr(func))
        self._overrides: Mapping[str, object] = dict(overrides or {})
        self._plan = plan_of(func, self._overrides)
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
        """This fixture's own `Depends(...)` sites, overrides already applied."""
        return self._plan

    @property
    def dependencies(self) -> tuple[Fixture[Any], ...]:
        """The fixture nodes this one depends on, for graph walking.

        Acyclic by construction — `Fixture.__init__` checks the moment a fixture is built, so
        every walker downstream (the scheduler, `with_()`, a future `--graph` dump) can recurse
        over this without a visited-set of its own.
        """
        return tuple(i.source for i in self._plan if isinstance(i.source, Fixture))

    def with_(self, **overrides: object) -> Fixture[T]:
        """Derive a fixture whose named direct dependencies are replaced.

        Each override is either another `Fixture` or a plain value. Overrides are part of the
        static graph, so validation and scheduling still see the truth.

        MVP: direct dependencies only. Deep override by dependency path is roadmap.
        """
        # REVIEW (design, decide before the runtime lands): `with_()` returns a fresh `Fixture`
        # every call, and `Fixture` defines neither `__eq__` nor `__hash__` — so identity is the
        # only key. Verified: `base.with_() is base.with_()` is False, and so is `==`.
        #
        # That is fine for `function`/`call` scope, but for `module`/`session` the cache key is
        # what makes sharing mean anything. Two modules writing the identical
        # `db.with_(url=TEST_URL)` get two distinct session-scoped fixtures and build the resource
        # twice — and worse, an `exclusive=` token attached to one is not attached to the other,
        # so the scheduler happily runs them concurrently against the resource they were meant to
        # serialise on. This is the kind of bug that shows up as a flake under load.
        #
        # Two ways out: give `Fixture` structural `__eq__`/`__hash__` over
        # `(func, scope, exclusive, resolved-overrides)` so equal derivations collide in the
        # cache, or memoise `with_()` per `(self, sorted-overrides)`. The former also makes the
        # static graph deduplicate, which the `--graph` dump will want anyway. Either way, note
        # that override *values* must be hashable for this — worth deciding now, since it
        # constrains what `Constant` may hold.
        injectable = {i.param for i in plan_of(self._func)}
        if unknown := sorted(set(overrides) - injectable):
            raise TypeError(
                f"{self._name}.with_(): no injected parameter named "
                f"{', '.join(repr(u) for u in unknown)}. "
                f"Injectable parameters are: {', '.join(sorted(injectable)) or '(none)'}."
            )
        rendered = ", ".join(f"{k}={_render(v)}" for k, v in overrides.items())
        return Fixture(
            self._func,
            scope=self._scope,
            exclusive=self._exclusive,
            name=f"{self._name}.with_({rendered})",
            overrides={**self._overrides, **overrides},
        )

    def __call__(self, *args: object, **kwargs: object) -> Any:
        """Call the underlying function directly, as ordinary Python.

        No injection is performed — any un-passed `Depends(...)` parameter keeps its sentinel
        default. This exists so a fixture stays a normal callable; the supported override
        mechanism is `with_()` at the call site.
        """
        return self._func(*args, **kwargs)

    def __repr__(self) -> str:
        exclusive = "" if self._exclusive is False else f" exclusive={self._exclusive!r}"
        return f"<Fixture {self._name} scope={self._scope}{exclusive}>"


def _render(value: object) -> str:
    return value.name if isinstance(value, Fixture) else repr(value)


def _check_acyclic(root: Fixture[Any]) -> None:
    """Raise if `root`'s dependency graph loops back on itself.

    A single `@velox.fixture()` decoration can never produce a cycle — to depend on a fixture it
    has to already exist as an object — but `with_()` or a future late rebind could, and the
    check belongs here once rather than in every future graph walker. Identity-keyed (`id()`),
    not `Fixture.__eq__`/`__hash__`: giving `Fixture` structural equality is the open question in
    `with_()`'s REVIEW block, and this must not force that decision.
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
