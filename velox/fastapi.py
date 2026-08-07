"""FastAPI support: per-test dependency overrides against the app you already have.

The FastAPI docs teach `app.dependency_overrides[dep] = fake` and a `.clear()` in teardown. Both
halves are per-app-instance state, so a suite that runs sixteen tests at once against one
module-level `app = FastAPI()` has sixteen tests writing one dict and one of them wiping it — the
most likely footgun in the reference stack. The usual escape is an app *factory*, which is a change
to production code made purely to suit the tests.

velox does not ask for that change. Read from source (`fastapi/dependencies/utils.py`,
`starlette/applications.py`, `httpx/_transports/asgi.py`):

- A route bakes in only a *pointer* to the app (`dependency_overrides_provider`). The override is
  read at request-solve time, and FastAPI's whole contract with the attribute is a truthiness check
  plus `.get(call, call)`.
- `request.app` is `scope["app"]`, which `Starlette.__call__` sets to the singleton on every
  request. `app.state` is one `State` — pure attribute delegation over a `_state` dict — built once
  and never reassigned upstream.
- `ASGITransport` awaits the app *in the calling task*, so a request inherits the caller's
  `contextvars.Context`. velox runs each test in a fresh one.

So the attribute can be swapped, once, for a proxy that layers a ContextVar of per-test overrides
over whatever the app already had. Concurrent tests each see their own layer through the same
singleton; nothing in the app changes; there is nothing to clean up. This is spec/08's
"the write is global, but the view doesn't have to be" applied to the one target where it is
cheap — a documented attribute with a two-method contract.

Importing this module requires `fastapi` and `httpx`. `velox/__init__.py` deliberately does not,
so the core package keeps zero hard dependencies; usage is `import velox.fastapi` or
`from velox import fastapi as velox_fastapi`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Mapping, MutableMapping
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Final, final
from weakref import WeakKeyDictionary

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import State

from velox._fixtures import Fixture, fixture

__all__ = ["client", "lifespan"]

type Override = Callable[..., Any]
"""A dependency callable, and what it is overridden by — FastAPI's key and value, unchanged."""


@final
class _Missing:
    """Sentinel for "no layer holds this key", distinct from `None`, which is a legal value."""

    __slots__ = ()


_MISSING: Final = _Missing()


# --------------------------------------------------------------------------------------
# The layer stack
# --------------------------------------------------------------------------------------


@final
class _Layers[K]:
    """A stack of per-test override mappings, held in a `ContextVar`.

    Innermost last, so a `client()` entered inside another one shadows it and both shadow the
    app's own values. One instance per proxy, so per app object: velox never puts two apps'
    overrides in one variable.
    """

    __slots__ = ("_var",)

    def __init__(self, name: str) -> None:
        self._var: ContextVar[tuple[dict[K, Any], ...]] = ContextVar(name, default=())

    def push(self, values: Mapping[K, Any]) -> Token[tuple[dict[K, Any], ...]]:
        """Add a layer to the current context. The mapping is copied; the caller keeps theirs."""
        return self._var.set((*self._var.get(), dict(values)))

    def top(self) -> dict[K, Any] | None:
        """The innermost layer, or `None` when this context is not inside a `client()`."""
        layers = self._var.get()
        return layers[-1] if layers else None

    def lookup(self, key: K) -> Any:
        """The innermost layer holding `key`, or `_MISSING`."""
        for layer in reversed(self._var.get()):
            if key in layer:
                return layer[key]
        return _MISSING

    def any(self) -> bool:
        """Whether any layer in this context holds anything. Cheap enough for a hot path."""
        return any(self._var.get())

    def merged(self, base: Mapping[K, Any]) -> dict[K, Any]:
        """`base` with every layer applied, outermost first. For iteration and sizing only."""
        merged = dict(base)
        for layer in self._var.get():
            merged.update(layer)
        return merged


# --------------------------------------------------------------------------------------
# The proxies
# --------------------------------------------------------------------------------------


@final
class _LayeredOverrides(MutableMapping[Override, Override]):
    """`app.dependency_overrides`, layered per test.

    FastAPI reads this at request-solve time, never at route-registration time, so installing the
    proxy after the routes are built still routes every subsequent request through the layer
    belonging to the task that made it.

    Reads consult the layers, then the app's own dict. Writes go to the innermost layer when one
    is active — which is what keeps a hand-written `app.dependency_overrides[dep] = f` inside a
    test working, and keeps it from being seen by the fifteen tests running alongside it.
    """

    __slots__ = ("_base", "_layers")

    def __init__(self, base: MutableMapping[Override, Override]) -> None:
        self._base = base
        self._layers: _Layers[Override] = _Layers("velox.fastapi.dependency_overrides")

    @property
    def base(self) -> MutableMapping[Override, Override]:
        """The dict the app was built with. Still the app's, still written to outside a test."""
        return self._base

    def push_layer(self, values: Mapping[Override, Override]) -> Token[Any]:
        """Add this context's overrides. Reset the token to drop them again."""
        return self._layers.push(values)

    def __getitem__(self, key: Override) -> Override:
        value = self._layers.lookup(key)
        return self._base[key] if value is _MISSING else value

    def __setitem__(self, key: Override, value: Override) -> None:
        layer = self._layers.top()
        if layer is None:
            self._base[key] = value
        else:
            layer[key] = value

    def __delitem__(self, key: Override) -> None:
        layer = self._layers.top()
        if layer is None:
            del self._base[key]
        elif key in layer:
            del layer[key]
        elif self._layers.lookup(key) is not _MISSING or key in self._base:
            raise RuntimeError(_DELETE_OUTSIDE_LAYER.format(key=_render(key)))
        else:
            raise KeyError(key)

    def __iter__(self) -> Iterator[Override]:
        return iter(self._layers.merged(self._base))

    def __len__(self) -> int:
        return len(self._layers.merged(self._base))

    def __bool__(self) -> bool:
        # FastAPI truth-tests this once per dependency per request before it reaches for `.get`,
        # so answer without materializing the merged view.
        return bool(self._base) or self._layers.any()

    def clear(self) -> None:
        """Drop what this test overrode; outside a test, what the app overrode.

        `app.dependency_overrides.clear()` is the teardown line every FastAPI testing tutorial
        ends on. Under velox it is a no-op you can delete — the layer is dropped when `client()`
        exits — but it keeps working, and it stays local, so a teardown copied in from the docs
        cannot wipe a concurrently-running test's overrides.
        """
        layer = self._layers.top()
        (self._base if layer is None else layer).clear()

    def __repr__(self) -> str:
        layer = self._layers.top()
        mine = 0 if layer is None else len(layer)
        return f"<velox dependency_overrides: {mine} in this test, {len(self._base)} on the app>"


@final
class _LayeredState(State):
    """`app.state`, layered per test.

    A `State` is attribute and item delegation over one `_state` dict, and upstream builds it once
    and never reassigns it — so a subclass sharing that same dict is a drop-in. Reads check the
    layers first; writes land in the innermost layer when one is active, and in the app's own
    state otherwise, which is where a lifespan's writes belong.
    """

    __slots__ = ("_layers",)

    def __init__(self, base: dict[str, Any]) -> None:
        super().__init__(base)
        # `State.__setattr__` writes into `_state`, so the layer stack has to go in behind it —
        # exactly as upstream installs `_state` itself.
        object.__setattr__(self, "_layers", _Layers("velox.fastapi.state"))

    def push_layer(self, values: Mapping[str, Any]) -> Token[Any]:
        """Add this context's state. Reset the token to drop it again."""
        return self._layers.push(values)

    def __getattr__(self, key: Any) -> Any:
        # Only reached when ordinary lookup fails, so `_state` and `_layers` never arrive here.
        value = self._layers.lookup(key)
        return super().__getattr__(key) if value is _MISSING else value

    def __setattr__(self, key: Any, value: Any) -> None:
        layer = self._layers.top()
        if layer is None:
            super().__setattr__(key, value)
        else:
            layer[key] = value

    def __delattr__(self, key: Any) -> None:
        layer = self._layers.top()
        if layer is None:
            super().__delattr__(key)
        elif key in layer:
            del layer[key]
        else:
            raise AttributeError(key)

    def __getitem__(self, key: str) -> Any:
        value = self._layers.lookup(key)
        return super().__getitem__(key) if value is _MISSING else value

    def __setitem__(self, key: str, value: Any) -> None:
        self.__setattr__(key, value)

    def __delitem__(self, key: str) -> None:
        self.__delattr__(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._layers.merged(self._state))

    def __len__(self) -> int:
        return len(self._layers.merged(self._state))


# --------------------------------------------------------------------------------------
# Installation
# --------------------------------------------------------------------------------------


@final
@dataclass(slots=True)
class _Install:
    """What velox has swapped in on one app. `state` stays `None` until someone asks for it."""

    overrides: _LayeredOverrides
    state: _LayeredState | None = None


_INSTALLS: Final[WeakKeyDictionary[FastAPI, _Install]] = WeakKeyDictionary()

_REPLACED = """\
velox.fastapi.client(): app.{attribute} was replaced after velox installed its per-test layer.

Something assigned to the attribute directly — almost always `app.{attribute} = {{}}` in a
teardown, the reset idiom from the FastAPI testing docs. That assignment threw away the layer
every concurrently-running test was reading through, so velox is stopping rather than running
them against the wrong dependencies.

Delete the reset. Under velox each `client()` context drops its own layer on exit and touches
nothing else, so there is nothing left to clean up.\
"""

_DELETE_OUTSIDE_LAYER = """\
velox.fastapi: cannot `del app.dependency_overrides[{key}]` from inside a test.

That override belongs to the app itself (or to an enclosing `client()`), not to this test, and
deleting it here would delete it for every test running alongside this one. Nothing needs
deleting: overrides this test set are dropped when its `client()` context exits.\
"""


def _install(app: FastAPI) -> _Install:
    """Swap in the overrides proxy, once per app object. Idempotent.

    velox is single-threaded async and nothing below suspends, so the check and the set cannot
    interleave with another test's — a lock here would be a claim about threads that velox does
    not make. Escalating rather than reinstalling on a replaced attribute is spec/00 I6: a
    silently re-established layer would lose whatever the assignment discarded.
    """
    record = _INSTALLS.get(app)
    if record is None:
        record = _Install(overrides=_LayeredOverrides(app.dependency_overrides))
        # Annotated `dict` upstream, but only ever *used* as a truthy `.get(call, call)`, which is
        # a `Mapping`. Narrowing the annotation is FastAPI's to do; the suppression is ours.
        app.dependency_overrides = record.overrides  # pyrefly: ignore[bad-assignment]
        _INSTALLS[app] = record
    elif app.dependency_overrides is not record.overrides:
        raise RuntimeError(_REPLACED.format(attribute="dependency_overrides"))
    return record


def _install_state(app: FastAPI, record: _Install) -> _LayeredState:
    """Swap in the state proxy, once per app object, over the app's *own* `_state` dict."""
    if record.state is None:
        record.state = _LayeredState(app.state._state)
        app.state = record.state
    elif app.state is not record.state:
        raise RuntimeError(_REPLACED.format(attribute="state"))
    return record.state


def _render(dependency: object) -> str:
    """A dependency callable's name, for an error message. Never raises."""
    return getattr(dependency, "__name__", None) or repr(dependency)


# --------------------------------------------------------------------------------------
# Public surface
# --------------------------------------------------------------------------------------


@asynccontextmanager
async def client(
    app: FastAPI,
    *,
    overrides: Mapping[Override, Override] | None = None,
    state: Mapping[str, Any] | None = None,
    base_url: str = "http://testserver",
) -> AsyncIterator[AsyncClient]:
    """An `httpx.AsyncClient` speaking to `app` in-process, with this test's overrides layered on.

    :param app: your real app — the module-level `app = FastAPI()`, not a per-test copy.
    :param overrides: `{dependency: replacement}`, with FastAPI's exact semantics. The value is a
        *dependency callable*, so a fixture value is passed as `lambda: session`. velox does not
        wrap non-callables for you: a silently-wrapped value would diverge from what the same
        dict means when written by hand.
    :param state: `{name: value}` layered over `app.state` for the duration. Passing it once for
        an app installs the state proxy for that app; contexts that never pass it read and write
        the app's own state, which is where a lifespan's writes belong.
    :param base_url: what relative request paths are resolved against.

    The overrides layer is pushed even when `overrides` is empty, so an `app.dependency_overrides`
    write inside the `async with` is this test's and no one else's.

    Nesting stacks: a `client()` entered while another is active sees the inner mappings first,
    then the outer ones, then the app's own — which is what makes a derived fixture that adds one
    override to a broader one behave the way it reads.

    No lifespan runs; `ASGITransport` never sends a lifespan scope, and velox does not fake one.
    For an app whose startup builds state the tests need, depend on `velox.fastapi.lifespan(app)`.
    """
    record = _install(app)
    tokens: list[Token[Any]] = []
    try:
        tokens.append(record.overrides.push_layer(overrides or {}))
        if state is not None:
            tokens.append(_install_state(app, record).push_layer(state))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=base_url) as http:
            yield http
    finally:
        # Innermost first, and by token rather than by re-setting a value, so an exception on the
        # way out cannot leave this context reading a layer that no longer belongs to it.
        for token in reversed(tokens):
            token.var.reset(token)


def lifespan(app: FastAPI) -> Fixture[FastAPI]:
    """A session-scoped fixture that runs `app`'s startup and shutdown around the whole run.

    `client()` deliberately does not do this — `ASGITransport` speaks HTTP scopes only, and a
    lifespan that really builds an engine and a connection pool is not something to run once per
    test. Depend on this where the app's startup is what puts the state under test in place::

        started = velox.fastapi.lifespan(app)

        async def test_it(_: FastAPI = Depends(started), c: AsyncClient = Depends(api_client)):
            ...

    Its writes land in the app's own state, since session setup runs outside any test's layer,
    which is exactly what makes them visible to every test.
    """

    @fixture(scope="session", name=f"lifespan({app.title!r})")
    async def started() -> AsyncIterator[FastAPI]:
        async with app.router.lifespan_context(app):
            yield app

    return started
