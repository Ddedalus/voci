"""The session dependency.

`get_session` is the seam tests override. It reads the sessionmaker off `app.state` rather than a
module global, so an app instance owns its own database wiring — which is what lets each test build
its own app (see `tests/fixtures.py::api_client`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with maker() as session:
        yield session
