"""Settings, read through an injectable mapping."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    endpoint: str = "https://hooks.example.com/v1"
    retries: int = 3
    cache_ttl: float = 30.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            endpoint=env.get("RELAY_ENDPOINT", cls.endpoint),
            retries=int(env.get("RELAY_RETRIES", cls.retries)),
            cache_ttl=float(env.get("RELAY_CACHE_TTL", cls.cache_ttl)),
        )
