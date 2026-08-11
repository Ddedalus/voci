"""Shared helpers for the assertion tests: `mock_config`, `callop`/`callequal`, and the
`rewritten` fixture.
"""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from velox import _rewrite
from velox._rewrite import Config, explanation_lines


def mock_config(
    verbose: int = 0,
    assertion_text_diff_style: str = "ndiff",
    truncation_limit_lines: int | None = 0,
    truncation_limit_chars: int | None = 0,
) -> Config:
    """Builds a `Config` with truncation off by default."""
    return Config(
        {
            "assertion_text_diff_style": assertion_text_diff_style,
            "truncation_limit_lines": truncation_limit_lines,
            "truncation_limit_chars": truncation_limit_chars,
        },
        verbosity=verbose,
    )


def callop(
    op: str,
    left: Any,
    right: Any,
    verbose: int = 0,
    assertion_text_diff_style: str = "ndiff",
) -> list[str] | None:
    """Ported from upstream `callop`; dispatches straight at velox's explanation entrypoint."""
    return explanation_lines(
        op,
        left,
        right,
        mock_config(verbose=verbose, assertion_text_diff_style=assertion_text_diff_style),
    )


def callequal(
    left: Any,
    right: Any,
    verbose: int = 0,
    assertion_text_diff_style: str = "ndiff",
) -> list[str] | None:
    """Ported from upstream `callequal`."""
    return callop("==", left, right, verbose, assertion_text_diff_style=assertion_text_diff_style)


@pytest.fixture
def rewritten(tmp_path: Path) -> Iterator[Callable[..., Any]]:
    """Write a module, import it through the rewriting hook, and hand back the module.
    Tears down the hook, `sys.path`, and `sys.modules` on exit.
    """
    installed: list[str] = []
    roots = tmp_path / "suite"
    roots.mkdir()
    cache = tmp_path / "cache"

    def build(source: str, *, name: str = "mod_under_test", **install_kwargs: Any) -> Any:
        path = roots / f"{name}.py"
        path.write_text(textwrap.dedent(source))
        _rewrite.install([roots], cache_dir=cache, **install_kwargs)
        sys.path.insert(0, str(roots))
        installed.append(name)
        return __import__(name)

    try:
        yield build
    finally:
        _rewrite.uninstall()
        for name in installed:
            sys.modules.pop(name, None)
        while str(roots) in sys.path:
            sys.path.remove(str(roots))
