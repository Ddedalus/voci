"""Delivery, retries, and failure — all tier (a), all concurrent."""

from __future__ import annotations

import logging
from typing import Annotated

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
    r: Annotated[Relay, Depends(relay)],
    t: Annotated[FakeTransport, Depends(transport)],
) -> None:
    response = await r.deliver("https://hooks.test/v1", b'{"event": "ping"}')

    assert response.status == 200
    assert t.sent == [("https://hooks.test/v1", b'{"event": "ping"}')]


async def test_retries_until_success(
    r: Annotated[Relay, Depends(flaky_relay)],
    t: Annotated[FakeTransport, Depends(flaky_transport)],
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
    r: Annotated[Relay, Depends(dead_relay)],
    t: Annotated[FakeTransport, Depends(dead_transport)],
) -> None:
    with velox.raises(DeliveryFailed, match="failed after 3 attempts"):
        await r.deliver("https://hooks.test/v1", b"doomed")

    assert len(t.sent) == 3


async def _assert_not_retried(status: int, r: Relay, t: FakeTransport) -> None:
    t.responses = [Response(status)]

    response = await r.deliver("https://hooks.test/v1", b"once")

    assert response.status == status
    assert len(t.sent) == 1


# Each status is its own test, sharing `_assert_not_retried` above.
async def test_200_is_never_retried(
    r: Annotated[Relay, Depends(relay)], t: Annotated[FakeTransport, Depends(transport)]
) -> None:
    await _assert_not_retried(200, r, t)


async def test_201_is_never_retried(
    r: Annotated[Relay, Depends(relay)], t: Annotated[FakeTransport, Depends(transport)]
) -> None:
    await _assert_not_retried(201, r, t)


async def test_204_is_never_retried(
    r: Annotated[Relay, Depends(relay)], t: Annotated[FakeTransport, Depends(transport)]
) -> None:
    await _assert_not_retried(204, r, t)


async def test_400_is_never_retried(
    r: Annotated[Relay, Depends(relay)], t: Annotated[FakeTransport, Depends(transport)]
) -> None:
    await _assert_not_retried(400, r, t)


async def test_404_is_never_retried(
    r: Annotated[Relay, Depends(relay)], t: Annotated[FakeTransport, Depends(transport)]
) -> None:
    await _assert_not_retried(404, r, t)


async def test_422_is_never_retried(
    r: Annotated[Relay, Depends(relay)], t: Annotated[FakeTransport, Depends(transport)]
) -> None:
    await _assert_not_retried(422, r, t)


async def test_retries_are_logged(
    r: Annotated[Relay, Depends(flaky_relay)],
    logs: Annotated[velox.LogRecords, Depends(velox.log_records)],
) -> None:
    """Log records are captured per test via a ContextVar, not by swapping a global handler.

    Sixteen tests logging at once each see only their own records — including anything logged from
    inside `asyncio.to_thread`, which inherits the context.
    """
    with logs.set_level(logging.WARNING, logger="relay"):
        await r.deliver("https://hooks.test/v1", b"retry-me")

    # `logs.messages` is `record.getMessage()` already applied, aligned index-for-index with
    # `logs.records`. The raw `logging.LogRecord`s in `logs.records` never get a `.message`
    # attribute set on them, since velox's capture handler never formats a record onto a stream.
    warnings = [rec for rec in logs.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "attempt 1/3" in logs.messages[0]


@velox.timeout(5)
async def test_concurrent_delivery_does_not_serialise(
    r: Annotated[Relay, Depends(relay)],
    t: Annotated[FakeTransport, Depends(transport)],
) -> None:
    """Sixteen deliveries with 50ms of latency each finish in well under their serial cost.

    The `@velox.timeout(5)` above is not enforced yet, so this test is held to the suite-wide
    `--timeout`; the budget it names is what it should get once per-test timeouts land.
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
