"""Coroutine tests, and the async fixtures only they reach.

Contributes the `async def` test case, the consumers of the root conftest's `async def` and
async-generator fixtures, and `@pytest.mark.anyio` — the mark an async runner reads to run a
coroutine at all, which the conversion drops because voci needs no telling. `anyio_backend` is
the one name that runner resolves as a fixture; spelled here with a single value so no test id
carries a backend suffix.
"""

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_async_fixtures(clock, channel):
    channel.append(clock)

    assert channel == [1000.0]


@pytest.mark.anyio
async def test_async_with_a_sync_fixture(connection):
    assert connection.startswith("connection(")
