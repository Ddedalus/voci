"""CI insurance for `velox.fastapi`, and for the upstream facts it is built on.

Two jobs. The first is to prove the mechanism end to end: concurrent contexts hitting *one*
module-level app see only their own dependency overrides and their own state. The second is to
fail loudly the day fastapi, starlette or httpx changes one of the three facts that make that
possible — `scope["app"]` is the singleton, the override is read per request, and the ASGI call
happens in the caller's `contextvars.Context`. Those are read from source in the module docstring
of `velox/fastapi.py`; these tests are what keep the reading true.

velox's own suite runs under pytest, which has no event loop of its own here — no `pytest-asyncio`,
no `anyio` plugin — so every async test is a `def` wrapped around one `asyncio.run`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient

import pytest
from velox import fastapi as velox_fastapi

# The point of the exercise: one module-level app, exactly as an application writes it, shared by
# every test in this file and by every concurrent context inside them.
app = FastAPI()

_GATE: ContextVar[asyncio.Barrier | None] = ContextVar("gate", default=None)
"""Lets a test hold every in-flight handler open until all of them have arrived."""

_MARKER: ContextVar[str] = ContextVar("marker", default="unset")
"""Set by a test, read inside a handler — the context-propagation canary."""


def flavor() -> str:
    """The dependency under override. Overridden, it never runs."""
    return "base"


@app.get("/flavor")
async def read_flavor(value: str = Depends(flavor)) -> dict[str, str]:
    if (gate := _GATE.get()) is not None:
        await gate.wait()
    return {"flavor": value}


@app.get("/settings")
async def read_settings(request: Request) -> dict[str, str]:
    return {"settings": request.app.state.settings}


@app.get("/canaries")
async def read_canaries(request: Request) -> dict[str, Any]:
    return {"is_singleton": request.app is app, "marker": _MARKER.get()}


app.state.settings = "base-settings"


def run(main: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """One event loop per test. `asyncio.run` also gives each test a fresh context."""
    return asyncio.run(main())


async def flavor_seen_by(**kwargs: Any) -> str:
    async with velox_fastapi.client(app, **kwargs) as http:
        return (await http.get("/flavor")).json()["flavor"]


# --------------------------------------------------------------------------------------
# 1. The mechanism: concurrent contexts, one app
# --------------------------------------------------------------------------------------


def test_concurrent_contexts_each_see_their_own_overrides() -> None:
    """Three tasks, three contexts, one singleton — and the requests genuinely overlap.

    `asyncio.create_task` (which `gather` uses) copies the current context, so each branch below
    is the isolation velox gives a test. The barrier holds all three handlers open at once, so a
    passing run cannot be an artifact of them having taken turns.
    """

    async def main() -> list[str]:
        gate = asyncio.Barrier(3)

        async def overridden(value: str) -> str:
            _GATE.set(gate)
            return await flavor_seen_by(overrides={flavor: lambda: value})

        async def untouched() -> str:
            _GATE.set(gate)
            async with velox_fastapi.client(app) as http:
                return (await http.get("/flavor")).json()["flavor"]

        async with asyncio.timeout(10):
            return list(await asyncio.gather(overridden("a"), overridden("b"), untouched()))

    assert run(main) == ["a", "b", "base"]


def test_a_context_with_no_layer_sees_the_app_untouched() -> None:
    """No `client()` anywhere: the proxy is installed, and still answers as the plain dict did."""

    async def main() -> str:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            return (await http.get("/flavor")).json()["flavor"]

    assert run(main) == "base"


def test_layers_nest_innermost_first() -> None:
    """A `client()` entered inside another stacks; the inner mapping wins, the outer still shows."""

    async def main() -> tuple[str, str, str]:
        async with velox_fastapi.client(app, overrides={flavor: lambda: "outer"}) as outer:
            first = (await outer.get("/flavor")).json()["flavor"]
            inner_seen = await flavor_seen_by(overrides={flavor: lambda: "inner"})
            after = (await outer.get("/flavor")).json()["flavor"]
        return first, inner_seen, after

    assert run(main) == ("outer", "inner", "outer")


def test_a_hand_written_override_stays_inside_the_test() -> None:
    """The idiom from the FastAPI docs, and the teardown it teaches, both kept local."""

    async def main() -> tuple[str, str]:
        async with velox_fastapi.client(app) as http:
            app.dependency_overrides[flavor] = lambda: "by hand"
            mine = (await http.get("/flavor")).json()["flavor"]
            app.dependency_overrides.clear()  # the docs' teardown: local, and now a no-op
            after_clear = (await http.get("/flavor")).json()["flavor"]
        return mine, after_clear

    assert run(main) == ("by hand", "base")
    assert dict(velox_fastapi._install(app).overrides.base) == {}, "the app's own dict is untouched"


def test_overrides_read_as_a_mapping() -> None:
    """`len`, `in`, iteration and truthiness all answer for the layer, not just `.get`."""

    async def main() -> tuple[bool, int, bool, bool, int]:
        outside = bool(app.dependency_overrides)
        async with velox_fastapi.client(app, overrides={flavor: lambda: "x"}):
            return (
                outside,
                len(app.dependency_overrides),
                flavor in app.dependency_overrides,
                bool(app.dependency_overrides),
                len(app.dependency_overrides.keys()),
            )

    assert run(main) == (False, 1, True, True, 1)


def test_deleting_an_override_this_test_did_not_set_is_refused() -> None:
    """I6: masking a shared entry per-test is not something this proxy can do, so it says so."""
    other = FastAPI()
    other.dependency_overrides[flavor] = lambda: "shipped with the app"

    async def main() -> None:
        async with velox_fastapi.client(other):
            with pytest.raises(RuntimeError, match=r"cannot `del app\.dependency_overrides"):
                del other.dependency_overrides[flavor]

    run(main)


# --------------------------------------------------------------------------------------
# 2. State
# --------------------------------------------------------------------------------------


def test_concurrent_contexts_each_see_their_own_state() -> None:
    async def main() -> list[str]:
        async def seen(value: str) -> str:
            async with velox_fastapi.client(app, state={"settings": value}) as http:
                return (await http.get("/settings")).json()["settings"]

        return list(await asyncio.gather(seen("left"), seen("right")))

    assert run(main) == ["left", "right"]
    assert app.state.settings == "base-settings", "the app's own state survives the layers"


def test_state_written_inside_a_layer_does_not_escape_it() -> None:
    async def main() -> str:
        async with velox_fastapi.client(app, state={"settings": "mine"}):
            app.state.extra = "scratch"
            assert app.state.extra == "scratch"
            return app.state.settings

    assert run(main) == "mine"
    assert not hasattr(app.state, "extra")
    assert app.state.settings == "base-settings"


def test_state_falls_through_to_the_app_for_keys_the_layer_lacks() -> None:
    app.state.shared = "from the app"
    try:

        async def main() -> tuple[str, str]:
            async with velox_fastapi.client(app, state={"settings": "mine"}):
                return app.state.shared, app.state["shared"]

        assert run(main) == ("from the app", "from the app")
    finally:
        del app.state.shared


def test_state_reports_a_missing_key_as_an_attribute_error() -> None:
    async def main() -> None:
        async with velox_fastapi.client(app, state={"settings": "mine"}):
            with pytest.raises(AttributeError):
                _ = app.state.nonexistent

    run(main)


# --------------------------------------------------------------------------------------
# 3. Upstream-contract canaries
# --------------------------------------------------------------------------------------


def fresh_app() -> FastAPI:
    """A second app carrying the same route, for tests that mutate installation state."""
    other = FastAPI()

    @other.get("/flavor")
    async def read(value: str = Depends(flavor)) -> dict[str, str]:
        return {"flavor": value}

    return other


def test_fastapi_only_needs_truthiness_and_get() -> None:
    """The narrow contract the whole design is sized against.

    This object is not a `Mapping`, has no `__getitem__`, no `__iter__`, no `__len__` — and a
    request still resolves through it. The day FastAPI reaches for anything else, this fails here
    rather than in an adopter's suite.
    """

    class Minimal:
        def __init__(self, mapping: dict[Any, Any]) -> None:
            self._mapping = mapping

        def __bool__(self) -> bool:
            return bool(self._mapping)

        def get(self, key: Any, default: Any = None) -> Any:
            return self._mapping.get(key, default)

    other = fresh_app()
    other.dependency_overrides = Minimal({flavor: lambda: "minimal"})  # pyrefly: ignore

    async def main() -> str:
        transport = ASGITransport(app=other)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            return (await http.get("/flavor")).json()["flavor"]

    assert run(main) == "minimal"


def test_app_state_is_one_object_delegating_to_one_dict() -> None:
    """`State` is attribute delegation over `_state`, and no request rebuilds or replaces it."""
    other = fresh_app()
    before = other.state
    other.state.thing = "written"

    assert before._state["thing"] == "written"
    assert other.state["thing"] == "written"

    async def main() -> None:
        transport = ASGITransport(app=other)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            await http.get("/flavor")

    run(main)
    assert other.state is before, "upstream never reassigns app.state, so a subclass survives"


def test_request_app_is_the_module_level_singleton() -> None:
    """`Starlette.__call__` does `scope["app"] = self`; `request.app` reads it back.

    This is why swapping an attribute on the singleton reaches every request, and why the app a
    handler sees is never a copy.
    """

    async def main() -> dict[str, Any]:
        async with velox_fastapi.client(app) as http:
            return (await http.get("/canaries")).json()

    assert run(main)["is_singleton"] is True


def test_the_handler_runs_in_the_callers_context() -> None:
    """`ASGITransport` awaits the app inline, so no context copy happens between the two.

    The entire design rests on this: a `ContextVar` set by the test is what the handler reads.
    """

    async def main() -> dict[str, Any]:
        _MARKER.set("set by the test")
        async with velox_fastapi.client(app) as http:
            return (await http.get("/canaries")).json()

    assert run(main)["marker"] == "set by the test"


def test_the_override_is_read_per_request_not_at_registration() -> None:
    """One client, two requests, two different override layers under the same routes."""

    async def main() -> tuple[str, str]:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            async with velox_fastapi.client(app, overrides={flavor: lambda: "first"}):
                first = (await http.get("/flavor")).json()["flavor"]
            async with velox_fastapi.client(app, overrides={flavor: lambda: "second"}):
                second = (await http.get("/flavor")).json()["flavor"]
        return first, second

    assert run(main) == ("first", "second")


def test_lifespan_is_not_run_by_client_but_is_available_as_a_fixture() -> None:
    started: list[str] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        started.append("up")
        yield
        started.append("down")

    other = FastAPI(lifespan=lifespan)

    async def main() -> None:
        async with velox_fastapi.client(other):
            pass
        assert started == [], "ASGITransport sends no lifespan scope, and velox fakes none"

        fixture = velox_fastapi.lifespan(other)
        agen = fixture.func()
        assert await anext(agen) is other
        assert started == ["up"]
        with pytest.raises(StopAsyncIteration):
            await anext(agen)
        assert started == ["up", "down"]

    run(main)
    assert velox_fastapi.lifespan(other).scope == "session"


# --------------------------------------------------------------------------------------
# 4. Installation and escalation
# --------------------------------------------------------------------------------------


def test_installation_is_idempotent() -> None:
    other = FastAPI()

    async def main() -> None:
        async with velox_fastapi.client(other):
            pass
        installed = other.dependency_overrides
        async with velox_fastapi.client(other):
            pass
        assert other.dependency_overrides is installed

    run(main)


def test_replacing_dependency_overrides_escalates() -> None:
    """The pytest-docs reset idiom, which would silently discard every concurrent test's layer."""
    other = FastAPI()

    async def main() -> None:
        async with velox_fastapi.client(other):
            pass
        other.dependency_overrides = {}  # what a copied-in teardown does
        with pytest.raises(RuntimeError, match="was replaced after velox installed"):
            async with velox_fastapi.client(other):
                pass

    run(main)


def test_replacing_state_escalates() -> None:
    from starlette.datastructures import State

    other = FastAPI()

    async def main() -> None:
        async with velox_fastapi.client(other, state={"a": 1}):
            pass
        other.state = State()
        with pytest.raises(RuntimeError, match=r"app\.state was replaced"):
            async with velox_fastapi.client(other, state={"a": 1}):
                pass

    run(main)


def test_state_is_left_alone_until_someone_asks_for_it() -> None:
    other = FastAPI()
    before = other.state

    async def main() -> None:
        async with velox_fastapi.client(other):
            pass

    run(main)
    assert other.state is before
