"""The session dependency.

`get_session` is the seam tests override, and being a named callable is the whole of what it takes
to be one: `app.dependency_overrides` is keyed by the function object. Reading the sessionmaker off
`app.state` rather than a module global is the same idea one level down — state that arrives
through the request is state a test can substitute (see `tests/fixtures.py::api_client`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with maker() as session:
        yield session
