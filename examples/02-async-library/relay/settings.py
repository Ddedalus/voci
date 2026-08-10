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
        # `defaults = cls()`, not `cls.endpoint`/`cls.retries`/`cls.cache_ttl` directly:
        # `slots=True` replaces each of those class attributes with a slot descriptor, not the
        # field's default value, so reading them off the class itself (rather than off an
        # instance) hands `env.get` a `member_descriptor` as its fallback -- silently wrong until
        # something tries to `int()`/`float()` it. An instance built with no arguments is real
        # values, every field.
        defaults = cls()
        return cls(
            endpoint=env.get("RELAY_ENDPOINT", defaults.endpoint),
            retries=int(env.get("RELAY_RETRIES", defaults.retries)),
            cache_ttl=float(env.get("RELAY_CACHE_TTL", defaults.cache_ttl)),
        )
