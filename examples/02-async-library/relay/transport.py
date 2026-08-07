"""The transport seam.

A `Protocol`, so the real implementation and the test double are interchangeable without either
knowing about the other. This is the single most valuable line of code in the example: it is what
makes tier (a) mocking possible and every patch in the suite unnecessary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes = b""


class Transport(Protocol):
    async def post(self, url: str, payload: bytes) -> Response: ...


@dataclass(slots=True)
class HttpTransport:
    """Stand-in for a real HTTP client."""

    timeout: float = 5.0

    async def post(self, url: str, payload: bytes) -> Response:
        raise NotImplementedError("wire up httpx/aiohttp here")


@dataclass(slots=True)
class FakeTransport:
    """A scripted transport. Records what it was asked to send, returns what it was told to.

    Deliberately hand-written rather than a `MagicMock`: it is shorter to read, it type-checks
    against `Transport`, and a signature change breaks it at the type checker instead of at 3am.
    Use `unittest.mock` when you want call assertions you would otherwise hand-roll.
    """

    responses: list[Response] = field(default_factory=list)
    sent: list[tuple[str, bytes]] = field(default_factory=list)
    latency: float = 0.0

    async def post(self, url: str, payload: bytes) -> Response:
        if self.latency:
            await asyncio.sleep(self.latency)
        self.sent.append((url, payload))
        if not self.responses:
            return Response(200)
        index = min(len(self.sent) - 1, len(self.responses) - 1)
        return self.responses[index]
