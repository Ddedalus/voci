"""`voci._affected.environment.placeholder_env_key`: the M3 stand-in for M4's real environment
key."""

from __future__ import annotations

import sys

from voci._affected.environment import placeholder_env_key


def test_placeholder_env_key_is_stable_within_one_interpreter() -> None:
    assert placeholder_env_key() == placeholder_env_key()


def test_placeholder_env_key_names_the_running_interpreter() -> None:
    key = placeholder_env_key()
    assert sys.implementation.name in key
    assert sys.platform in key
