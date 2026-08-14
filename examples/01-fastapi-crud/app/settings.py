"""Application configuration, as a frozen dataclass a test can hand around as a value."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    signup_bonus_cents: int = 0
    max_orders_per_user: int = 100

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Settings read from `env`, which defaults to the process environment.

        Production passes nothing; `tests/test_orders.py` passes a dict.
        """
        env = os.environ if env is None else env
        return cls(
            database_url=env.get("DATABASE_URL", "sqlite+aiosqlite:///./app.sqlite"),
            signup_bonus_cents=int(env.get("SIGNUP_BONUS_CENTS", "0")),
            max_orders_per_user=int(env.get("MAX_ORDERS_PER_USER", "100")),
        )
