"""Webhook delivery with retry and jittered backoff.

Note the asymmetry with `cache.py`, which is the whole point of this example:

- `transport` is injected  -> tests substitute it, fully concurrent, tier (a).
- `random.uniform` is a module-level import used inline -> the only way to make the jitter
  deterministic is `mock.patch("relay.client.random.uniform")`, which is a process-global write,
  which means the test runs solo and drains the suite.

The fix is not a better mocking library. The fix is one more constructor argument.
"""

from __future__ import annotations

import asyncio
import logging
import random

from relay.transport import Response, Transport

log = logging.getLogger("relay.client")


class DeliveryFailed(Exception):
    def __init__(self, url: str, attempts: int, last_status: int) -> None:
        super().__init__(f"delivery to {url} failed after {attempts} attempts (last {last_status})")
        self.url = url
        self.attempts = attempts
        self.last_status = last_status


def backoff_schedule(base_delay: float, attempts: int) -> list[float]:
    """Exponential backoff, no jitter. Pure function, trivially testable."""
    return [base_delay * 2**i for i in range(attempts)]


class Relay:
    def __init__(
        self,
        transport: Transport,
        *,
        retries: int = 3,
        base_delay: float = 0.05,
    ) -> None:
        self._transport = transport
        self._retries = retries
        self._base_delay = base_delay

    async def deliver(self, url: str, payload: bytes) -> Response:
        last_status = 0
        for attempt, delay in enumerate(backoff_schedule(self._base_delay, self._retries), start=1):
            response = await self._transport.post(url, payload)
            last_status = response.status
            if response.status < 500:
                return response
            log.warning(
                "delivery attempt %d/%d to %s failed with %d",
                attempt,
                self._retries,
                url,
                response.status,
            )
            if attempt < self._retries:
                await asyncio.sleep(delay * random.uniform(0.5, 1.5))
        raise DeliveryFailed(url, self._retries, last_status)

    async def deliver_all(self, url: str, payloads: list[bytes]) -> list[Response]:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(self.deliver(url, p)) for p in payloads]
        return [t.result() for t in tasks]
