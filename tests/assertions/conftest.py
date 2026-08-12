"""Fixtures for the assertion tests. Plain helpers live in `_support.py`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from ._support import imported_module


@pytest.fixture(autouse=True)
def _no_ci_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explanation truncation reads `CI`/`BUILD_NUMBER` (see `_vendor._shim.running_on_ci`);
    strip them so these tests don't flip behavior depending on where they're run.
    """
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("BUILD_NUMBER", raising=False)


@pytest.fixture
def rewritten(tmp_path: Path) -> Iterator[Callable[..., Any]]:
    """Write a module, import it through the rewriting hook, and hand back the module.
    Tears down the hook, `sys.path`, and `sys.modules` on exit.
    """
    with imported_module(tmp_path, rewrite=True) as build:
        yield build


@pytest.fixture
def unrewritten(tmp_path: Path) -> Iterator[Callable[..., Any]]:
    """Write a module and import it directly, bypassing the rewriting hook."""
    with imported_module(tmp_path, rewrite=False) as build:
        yield build
