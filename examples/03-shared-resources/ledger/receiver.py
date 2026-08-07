"""A webhook receiver that binds a real port.

The other archetypal exclusive resource: an operating-system-level singleton. Two of these on the
same port is an `OSError`, not a flaky test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

WEBHOOK_PORT = 8099


@dataclass(slots=True)
class Receiver:
    port: int = WEBHOOK_PORT
    received: list[bytes] = field(default_factory=list)
    _server: asyncio.Server | None = field(default=None, repr=False)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(4096)
        self.received.append(data)
        writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def send(port: int, payload: bytes) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    await reader.read(4096)
    writer.close()
    await writer.wait_closed()
