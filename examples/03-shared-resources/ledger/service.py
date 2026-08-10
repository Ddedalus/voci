"""The async surface over the blocking store."""

from __future__ import annotations

import asyncio

from ledger.flags import is_enabled
from ledger.store import Entry, Store


class LedgerService:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def append(self, account: str, cents: int) -> int:
        return await asyncio.to_thread(self._store.append, account, cents)

    async def balance(self, account: str) -> int:
        return await asyncio.to_thread(self._store.balance, account)

    async def entries(self, account: str) -> list[Entry]:
        return await asyncio.to_thread(self._store.entries, account)

    async def total(self) -> int:
        return await asyncio.to_thread(self._store.total)

    async def transfer(self, source: str, target: str, cents: int) -> None:
        if is_enabled("strict_transfers") and await self.balance(source) < cents:
            raise ValueError(f"insufficient balance in {source}")
        await self.append(source, -cents)
        await self.append(target, cents)

    def balance_blocking(self, account: str) -> int:
        """The trap.

        A perfectly ordinary-looking synchronous accessor. Call it from a coroutine and it blocks
        the event loop — every other test in flight stops dead until sqlite comes back, and nothing
        in the code reads as wrong. `tests/test_safety.py` calls it directly to show the effect.
        """
        return self._store.balance(account)
