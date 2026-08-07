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
from typing import Any, Literal, Protocol, cast, final, overload

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


def _injection(
    param: str, default: object, overrides: Mapping[str, object], *, keyword_only: bool
) -> Injection | None:
    if not isinstance(default, Dependency):
        return None
    source = overrides.get(param, default.fixture) if param in overrides else default.fixture
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
        """The fixture nodes this one depends on, for graph walking."""
        return tuple(i.source for i in self._plan if isinstance(i.source, Fixture))

    def with_(self, **overrides: object) -> Fixture[T]:
        """Derive a fixture whose named direct dependencies are replaced.

        Each override is either another `Fixture` or a plain value. Overrides are part of the
        static graph, so validation and scheduling still see the truth.

        MVP: direct dependencies only. Deep override by dependency path is roadmap.
        """
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
