"""The request-scoped database session.

`get_session` is the seam tests substitute, and a named callable is all it takes to be one:
`app.dependency_overrides` is keyed by the function object. The sessionmaker it reads lives on
`app.state`, which a test can layer the same way (see `tests/fixtures.py::api_client`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with maker() as session:
        yield session
