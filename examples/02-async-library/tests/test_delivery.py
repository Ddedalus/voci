"""Delivery, retries, and failure — all tier (a), all concurrent."""

from __future__ import annotations

import logging

import velox
from velox import Depends

from relay.client import DeliveryFailed, Relay, backoff_schedule
from relay.transport import FakeTransport, Response
from tests.fixtures import (
    dead_relay,
    dead_transport,
    flaky_relay,
    flaky_transport,
    relay,
    transport,
)


async def test_successful_delivery(
    r: Relay = Depends(relay),
    t: FakeTransport = Depends(transport),
) -> None:
    response = await r.deliver("https://hooks.test/v1", b'{"event": "ping"}')

    assert response.status == 200
    assert t.sent == [("https://hooks.test/v1", b'{"event": "ping"}')]


async def test_retries_until_success(
    r: Relay = Depends(flaky_relay),
    t: FakeTransport = Depends(flaky_transport),
) -> None:
    """`flaky_relay` (`tests/fixtures.py`) swaps the transport for this test only.

    Two things worth noticing. First, `flaky_relay` and `Depends(flaky_transport)` resolve to the
    *same instance* — a fixture is constructed once per test regardless of how many paths reach
    it, so the relay under test and the transport being asserted on are the same object. Second,
    nothing was patched, so this test runs alongside fifteen others.
    """
    response = await r.deliver("https://hooks.test/v1", b"retry-me")

    assert response.status == 200
    assert len(t.sent) == 3


async def test_gives_up_after_configured_retries(
    r: Relay = Depends(dead_relay),
    t: FakeTransport = Depends(dead_transport),
) -> None:
    with velox.raises(DeliveryFailed, match="failed after 3 attempts"):
        await r.deliver("https://hooks.test/v1", b"doomed")

    assert len(t.sent) == 3


@velox.parametrize("status", [200, 201, 204, 400, 404, 422])
async def test_non_5xx_is_never_retried(
    status: int,
    r: Relay = Depends(relay),
    t: FakeTransport = Depends(transport),
) -> None:
    t.responses = [Response(status)]

    response = await r.deliver("https://hooks.test/v1", b"once")

    assert response.status == status
    assert len(t.sent) == 1


async def test_retries_are_logged(
    r: Relay = Depends(flaky_relay),
    logs: velox.LogRecords = Depends(velox.log_records),
) -> None:
    """Log records are captured per test via a ContextVar, not by swapping a global handler.

    Sixteen tests logging at once each see only their own records — including anything logged from
    inside `asyncio.to_thread`, which inherits the context.
    """
    with logs.set_level(logging.WARNING, logger="relay"):
        await r.deliver("https://hooks.test/v1", b"retry-me")

    warnings = [rec for rec in logs.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "attempt 1/3" in warnings[0].message


@velox.timeout(5)
async def test_concurrent_delivery_does_not_serialise(
    r: Relay = Depends(relay),
    t: FakeTransport = Depends(transport),
) -> None:
    """`@velox.timeout` overrides the suite default for this test.

    The timeout is an `asyncio.timeout` around the whole test envelope, so it also covers setup
    and teardown, and a test that blows it is reported as `timeout` rather than as a generic
    failure.
    """
    t.latency = 0.05

    responses = await r.deliver_all("https://hooks.test/v1", [b"a", b"b", b"c", b"d"])

    assert [x.status for x in responses] == [200, 200, 200, 200]
    assert len(t.sent) == 4


def test_backoff_schedule_grows_exponentially() -> None:
    """A sync test. Runs on the context-propagating executor, holds one concurrency slot."""
    assert backoff_schedule(0.1, 4) == [0.1, 0.2, 0.4, 0.8]


def test_backoff_schedule_is_empty_for_zero_attempts() -> None:
    assert backoff_schedule(0.1, 0) == []
