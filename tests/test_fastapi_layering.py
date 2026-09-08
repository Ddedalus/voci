"""Tests for `voci.fastapi`: concurrent dependency-override and state isolation over one
module-level app, plus the upstream facts that isolation depends on -- `scope["app"]` is the
singleton, the override is read per request, and the ASGI call happens in the caller's
`contextvars.Context`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import pytest
from _support import run_async as run
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient

from voci import fastapi as voci_fastapi

# One module-level app, exactly as an application writes it, shared by every test in this file.
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


async def flavor_seen_by(**kwargs: Any) -> str:
    async with voci_fastapi.client(app, **kwargs) as http:
        return (await http.get("/flavor")).json()["flavor"]


# --------------------------------------------------------------------------------------
# 1. The mechanism: concurrent contexts, one app
# --------------------------------------------------------------------------------------


def test_concurrent_contexts_each_see_their_own_overrides() -> None:
    """Three tasks, three contexts, one singleton -- and the requests genuinely overlap: a
    barrier holds all three handlers open at once."""

    async def main() -> list[str]:
        gate = asyncio.Barrier(3)

        async def overridden(value: str) -> str:
            _GATE.set(gate)
            return await flavor_seen_by(overrides={flavor: lambda: value})

        async def untouched() -> str:
            _GATE.set(gate)
            async with voci_fastapi.client(app) as http:
                return (await http.get("/flavor")).json()["flavor"]

        async with asyncio.timeout(10):
            return list(await asyncio.gather(overridden("a"), overridden("b"), untouched()))

    assert run(main()) == ["a", "b", "base"]


def test_a_context_with_no_layer_sees_the_app_untouched() -> None:
    """No `client()` anywhere: the app answers as a plain, un-layered `FastAPI` would."""

    async def main() -> str:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            return (await http.get("/flavor")).json()["flavor"]

    assert run(main()) == "base"


def test_layers_nest_innermost_first() -> None:
    """A `client()` entered inside another stacks; the inner mapping wins, the outer still shows."""

    async def main() -> tuple[str, str, str]:
        async with voci_fastapi.client(app, overrides={flavor: lambda: "outer"}) as outer:
            first = (await outer.get("/flavor")).json()["flavor"]
            inner_seen = await flavor_seen_by(overrides={flavor: lambda: "inner"})
            after = (await outer.get("/flavor")).json()["flavor"]
        return first, inner_seen, after

    assert run(main()) == ("outer", "inner", "outer")


def test_a_hand_written_override_stays_inside_the_test() -> None:
    """The idiom from the FastAPI docs, and the teardown it teaches, both kept local."""

    async def main() -> tuple[str, str]:
        async with voci_fastapi.client(app) as http:
            app.dependency_overrides[flavor] = lambda: "by hand"
            mine = (await http.get("/flavor")).json()["flavor"]
            app.dependency_overrides.clear()  # the docs' teardown, applied to the local layer
            after_clear = (await http.get("/flavor")).json()["flavor"]
        return mine, after_clear

    assert run(main()) == ("by hand", "base")
    assert dict(voci_fastapi._install(app).overrides.base) == {}, "the app's own dict is untouched"


def test_overrides_read_as_a_mapping() -> None:
    """`len`, `in`, iteration and truthiness all answer for the layer, not just `.get`."""

    async def main() -> tuple[bool, int, bool, bool, int]:
        outside = bool(app.dependency_overrides)
        async with voci_fastapi.client(app, overrides={flavor: lambda: "x"}):
            return (
                outside,
                len(app.dependency_overrides),
                flavor in app.dependency_overrides,
                bool(app.dependency_overrides),
                len(app.dependency_overrides.keys()),
            )

    assert run(main()) == (False, 1, True, True, 1)


def test_deleting_an_override_this_test_did_not_set_is_refused() -> None:
    """Masking a shared entry per-test is not something this proxy can do, so it raises."""
    other = FastAPI()
    other.dependency_overrides[flavor] = lambda: "shipped with the app"

    async def main() -> None:
        async with voci_fastapi.client(other):
            with pytest.raises(RuntimeError, match=r"cannot `del app\.dependency_overrides"):
                del other.dependency_overrides[flavor]

    run(main())


# --------------------------------------------------------------------------------------
# 2. State
# --------------------------------------------------------------------------------------


def test_concurrent_contexts_each_see_their_own_state() -> None:
    async def main() -> list[str]:
        async def seen(value: str) -> str:
            async with voci_fastapi.client(app, state={"settings": value}) as http:
                return (await http.get("/settings")).json()["settings"]

        return list(await asyncio.gather(seen("left"), seen("right")))

    assert run(main()) == ["left", "right"]
    assert app.state.settings == "base-settings", "the app's own state survives the layers"


def test_state_written_inside_a_layer_does_not_escape_it() -> None:
    async def main() -> str:
        async with voci_fastapi.client(app, state={"settings": "mine"}):
            app.state.extra = "scratch"
            assert app.state.extra == "scratch"
            return app.state.settings

    assert run(main()) == "mine"
    assert not hasattr(app.state, "extra")
    assert app.state.settings == "base-settings"


def test_state_falls_through_to_the_app_for_keys_the_layer_lacks() -> None:
    app.state.shared = "from the app"
    try:

        async def main() -> tuple[str, str]:
            async with voci_fastapi.client(app, state={"settings": "mine"}):
                return app.state.shared, app.state["shared"]

        assert run(main()) == ("from the app", "from the app")
    finally:
        del app.state.shared


def test_state_reports_a_missing_key_as_an_attribute_error() -> None:
    async def main() -> None:
        async with voci_fastapi.client(app, state={"settings": "mine"}):
            with pytest.raises(AttributeError):
                _ = app.state.nonexistent

    run(main())


def test_concurrent_bare_client_contexts_do_not_leak_state_writes() -> None:
    """Two contexts, neither passing `state=`, each writing the same attribute: neither write
    may reach the other, or the app."""
    other = FastAPI()

    async def main() -> list[str]:
        gate = asyncio.Barrier(2)

        async def write(value: str) -> str:
            async with voci_fastapi.client(other):
                other.state.cache = value
                await gate.wait()  # both contexts hold their write open at once
                return other.state.cache

        return list(await asyncio.gather(write("a"), write("b")))

    assert run(main()) == ["a", "b"]
    assert not hasattr(other.state, "cache"), "neither in-test write should have reached the app"


# --------------------------------------------------------------------------------------
# 2b. Copying app.state
# --------------------------------------------------------------------------------------


def test_copy_of_layered_state_is_a_plain_state_with_the_merged_view() -> None:
    """`copy.copy(app.state)` returns a plain `State` snapshotting the merged view, not the
    proxy."""
    import copy

    from starlette.datastructures import State

    other = fresh_app()
    other.state.base_value = "from the app"

    async def main() -> tuple[str, str, bool]:
        async with voci_fastapi.client(other, state={"layered": "from the layer"}):
            copied = copy.copy(other.state)
            return copied.base_value, copied.layered, isinstance(copied, State)

    base_value, layered, is_plain_state = run(main())
    assert (base_value, layered) == ("from the app", "from the layer")
    assert is_plain_state
    assert type(copy.copy(other.state)) is State, "not the proxy — a plain State"


def test_deepcopy_of_layered_state_no_longer_raises() -> None:
    """`copy.deepcopy(app.state)` deep-copies only the merged values, never the `ContextVar`
    itself, and the result is independent of the original."""
    import copy

    other = fresh_app()
    other.state.nested = {"count": 1}

    async def main() -> dict[str, Any]:
        async with voci_fastapi.client(other):
            copied = copy.deepcopy(other.state)
            other.state.nested["count"] = 2  # mutate the original's dict after copying
            return copied.nested

    assert run(main()) == {"count": 1}, "the deep copy must not share the original's nested dict"


def test_bare_new_state_reads_raise_attribute_error_not_recursion_error() -> None:
    """`_LayeredState.__new__` with no `__init__` ever run leaves both `_layers` and `_state`
    unset; an attribute read on it raises a plain `AttributeError`."""
    bare = voci_fastapi._LayeredState.__new__(voci_fastapi._LayeredState)
    with pytest.raises(AttributeError):
        _ = bare.anything


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
    """`dependency_overrides` need not be a `Mapping` -- an object with only `__bool__` and
    `get` resolves a request just as well."""

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

    assert run(main()) == "minimal"


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

    run(main())
    assert other.state is before, "upstream never reassigns app.state, so a subclass survives"


def test_request_app_is_the_module_level_singleton() -> None:
    """`Starlette.__call__` does `scope["app"] = self`; `request.app` reads it back.

    This is why swapping an attribute on the singleton reaches every request, and why the app a
    handler sees is never a copy.
    """

    async def main() -> dict[str, Any]:
        async with voci_fastapi.client(app) as http:
            return (await http.get("/canaries")).json()

    assert run(main())["is_singleton"] is True


def test_the_handler_runs_in_the_callers_context() -> None:
    """`ASGITransport` awaits the app inline, so no context copy happens between the two.

    The entire design rests on this: a `ContextVar` set by the test is what the handler reads.
    """

    async def main() -> dict[str, Any]:
        _MARKER.set("set by the test")
        async with voci_fastapi.client(app) as http:
            return (await http.get("/canaries")).json()

    assert run(main())["marker"] == "set by the test"


def test_the_override_is_read_per_request_not_at_registration() -> None:
    """One client, two requests, two different override layers under the same routes."""

    async def main() -> tuple[str, str]:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            async with voci_fastapi.client(app, overrides={flavor: lambda: "first"}):
                first = (await http.get("/flavor")).json()["flavor"]
            async with voci_fastapi.client(app, overrides={flavor: lambda: "second"}):
                second = (await http.get("/flavor")).json()["flavor"]
        return first, second

    assert run(main()) == ("first", "second")


def test_lifespan_is_not_run_by_client_but_is_available_as_a_fixture() -> None:
    started: list[str] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        started.append("up")
        yield
        started.append("down")

    other = FastAPI(lifespan=lifespan)

    async def main() -> None:
        async with voci_fastapi.client(other):
            pass
        assert started == [], "ASGITransport sends no lifespan scope, and voci fakes none"

        fixture = voci_fastapi.lifespan(other)
        agen = fixture.func()
        assert await anext(agen) is other
        assert started == ["up"]
        with pytest.raises(StopAsyncIteration):
            await anext(agen)
        assert started == ["up", "down"]

    run(main())
    assert voci_fastapi.lifespan(other).scope == "session"


def test_lifespan_is_memoised_per_app() -> None:
    """`lifespan(app) is lifespan(app)`: two calls for the same app return the identical
    session-scoped fixture, so startup and shutdown run once per run, not once per call site."""
    other = FastAPI()

    first = voci_fastapi.lifespan(other)
    second = voci_fastapi.lifespan(other)

    assert first is second

    other_app = FastAPI()
    assert voci_fastapi.lifespan(other_app) is not first, "memoisation is per app, not global"


# --------------------------------------------------------------------------------------
# 4. Installation and escalation
# --------------------------------------------------------------------------------------


def test_installation_is_idempotent() -> None:
    other = FastAPI()

    async def main() -> None:
        async with voci_fastapi.client(other):
            pass
        installed = other.dependency_overrides
        async with voci_fastapi.client(other):
            pass
        assert other.dependency_overrides is installed

    run(main())


def test_replacing_dependency_overrides_escalates() -> None:
    """The pytest-docs reset idiom, which would silently discard every concurrent test's layer."""
    other = FastAPI()

    async def main() -> None:
        async with voci_fastapi.client(other):
            pass
        other.dependency_overrides = {}  # what a copied-in teardown does
        with pytest.raises(RuntimeError, match="was replaced after voci installed"):
            async with voci_fastapi.client(other):
                pass

    run(main())


def test_replacing_state_escalates() -> None:
    from starlette.datastructures import State

    other = FastAPI()

    async def main() -> None:
        async with voci_fastapi.client(other, state={"a": 1}):
            pass
        other.state = State()
        with pytest.raises(RuntimeError, match=r"app\.state was replaced"):
            async with voci_fastapi.client(other, state={"a": 1}):
                pass

    run(main())


def test_uninstall_restores_the_objects_voci_replaced() -> None:
    """`uninstall` puts back the `dependency_overrides` dict and `state` object the app was
    built with, by identity."""
    other = FastAPI()
    original_overrides = other.dependency_overrides

    async def main() -> None:
        async with voci_fastapi.client(other, overrides={flavor: lambda: "x"}, state={"a": 1}):
            pass

    run(main())
    original_state = voci_fastapi._install(other).original_state
    assert other.dependency_overrides is not original_overrides
    assert isinstance(other.state, voci_fastapi._LayeredState)

    voci_fastapi.uninstall(other)

    assert other.dependency_overrides is original_overrides
    assert other.state is original_state
    assert other not in voci_fastapi._INSTALLS


def test_uninstall_is_a_no_op_on_an_app_that_was_never_installed() -> None:
    voci_fastapi.uninstall(FastAPI())  # must not raise


def test_uninstall_is_idempotent() -> None:
    other = FastAPI()

    async def main() -> None:
        async with voci_fastapi.client(other):
            pass

    run(main())
    voci_fastapi.uninstall(other)
    voci_fastapi.uninstall(other)  # must not raise the second time


def test_uninstall_then_client_reinstalls_cleanly() -> None:
    """Uninstalling doesn't leave the app in the escalated "was replaced" state — a fresh
    `client()` afterward installs a brand new layer rather than raising."""
    other = fresh_app()

    async def main() -> str:
        async with voci_fastapi.client(other):
            pass
        voci_fastapi.uninstall(other)
        overrides = {flavor: lambda: "after uninstall"}
        async with voci_fastapi.client(other, overrides=overrides) as http:
            return (await http.get("/flavor")).json()["flavor"]

    assert run(main()) == "after uninstall"


def test_uninstall_drops_the_memoised_lifespan_fixture() -> None:
    other = FastAPI()
    first = voci_fastapi.lifespan(other)

    voci_fastapi.uninstall(other)

    assert voci_fastapi.lifespan(other) is not first


def test_state_installs_on_first_client_call_even_without_state_kwarg() -> None:
    """The proxy installs on first `client()` entry regardless of whether `state=` was
    passed."""
    other = FastAPI()
    before = other.state

    async def main() -> None:
        async with voci_fastapi.client(other):
            pass

    run(main())
    assert other.state is not before
    assert isinstance(other.state, voci_fastapi._LayeredState)


def test_lifespan_alone_never_installs_anything() -> None:
    """Calling `lifespan(app)` without ever entering `client()` must not swap in the
    overrides/state proxy -- that install is `client()`'s job."""
    other = FastAPI()
    before_state = other.state
    before_overrides = other.dependency_overrides

    voci_fastapi.lifespan(other)

    assert other.state is before_state
    assert other.dependency_overrides is before_overrides
