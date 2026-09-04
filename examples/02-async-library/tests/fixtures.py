"""Fixtures for the relay suite. Stdlib only."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from relay.cache import FakeClock, TTLCache
from relay.client import Relay
from relay.settings import Settings
from relay.transport import FakeTransport, Response

import velox
from velox import Depends


@velox.fixture()
def settings() -> Settings:
    """Configuration as a value.

    Because `Settings.from_env` takes the mapping as a parameter, no test in this suite ever needs
    `monkeypatch.setenv` or `mock.patch.dict(os.environ, ...)`. See `test_patching.py` for what
    the alternative costs.
    """
    return Settings(endpoint="https://hooks.test/v1", retries=3, cache_ttl=30.0)


@velox.fixture()
def transport() -> FakeTransport:
    return FakeTransport()


@velox.fixture()
def flaky_transport() -> FakeTransport:
    """Fails twice with 503, then succeeds."""
    return FakeTransport(responses=[Response(503), Response(503), Response(200, b"ok")])


@velox.fixture()
def dead_transport() -> FakeTransport:
    return FakeTransport(responses=[Response(500)])


@velox.fixture()
def relay(
    transport: Annotated[FakeTransport, Depends(transport)],
    settings: Annotated[Settings, Depends(settings)],
) -> Relay:
    """A relay with a very small delay so retry tests stay fast.

    Sync fixture, async consumers — that mixes freely. velox only cares whether the callable is a
    function, a coroutine function, or a generator of either kind.
    """
    return Relay(transport, retries=settings.retries, base_delay=0.001)


@velox.fixture()
def flaky_relay(
    transport: Annotated[FakeTransport, Depends(flaky_transport)],
    settings: Annotated[Settings, Depends(settings)],
) -> Relay:
    """`relay`, rebuilt with `flaky_transport` in place of `transport`.

    A sibling fixture, built exactly like `relay`, with one dependency swapped by hand.
    """
    return Relay(transport, retries=settings.retries, base_delay=0.001)


@velox.fixture()
def dead_relay(
    transport: Annotated[FakeTransport, Depends(dead_transport)],
    settings: Annotated[Settings, Depends(settings)],
) -> Relay:
    """`relay`, rebuilt with `dead_transport` in place of `transport`. See `flaky_relay`."""
    return Relay(transport, retries=settings.retries, base_delay=0.001)


@velox.fixture()
def clock() -> FakeClock:
    return FakeClock()


@velox.fixture()
def cache(
    clock: Annotated[FakeClock, Depends(clock)],
    settings: Annotated[Settings, Depends(settings)],
) -> TTLCache:
    return TTLCache(ttl=settings.cache_ttl, clock=clock)


@velox.fixture(scope="session")
async def audit_log() -> AsyncIterator[list[str]]:
    """A session-scoped fixture, to show the shape.

    Constructed once on first use — under a single-flight guard, so a hundred tests demanding it
    simultaneously produce exactly one construction — and torn down after the last test that
    depends on it finishes, by refcount rather than by a stack unwind.

    Mutable session state shared across concurrent tests is a hazard in general. A list you only
    append to is fine; a dict you read-modify-write is not.
    """
    entries: list[str] = []
    try:
        yield entries
    finally:
        entries.clear()
