"""Application configuration.

A plain frozen dataclass rather than module-level constants, because that is what makes
configuration *injectable* — see the `premium_settings` test in `tests/test_orders.py`. Every
setting read from a global is a setting that eventually gets patched, and every patch is a test
that runs solo.
"""

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
        """Read settings from a mapping that *defaults* to the environment.

        Taking `env` as a parameter is the whole trick: production passes nothing, tests pass a
        dict. No `monkeypatch.setenv`, no `mock.patch.dict`, no solo scheduling.
        """
        env = os.environ if env is None else env
        return cls(
            database_url=env.get("DATABASE_URL", "sqlite+aiosqlite:///./app.sqlite"),
            signup_bonus_cents=int(env.get("SIGNUP_BONUS_CENTS", "0")),
            max_orders_per_user=int(env.get("MAX_ORDERS_PER_USER", "100")),
        )
