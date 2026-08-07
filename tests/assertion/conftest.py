"""Shared helpers for the assertion tests.

velox's own suite still runs under pytest (see `justfile`), so these tests are written in
pytest's idiom. That is a happy accident for the ported ones: upstream's helpers translate
almost verbatim, which is the whole point of vendoring rather than reimplementing (spec/07 §1).
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
    """velox's stand-in for upstream's `mock_config`.

    Truncation defaults to off, matching upstream, so a diff assertion in a test is not
    quietly clipped. velox needs no terminal-writer or plugin-manager doubles: there is no hook
    dispatch to fake, so the shim `Config` is already the real thing.
    """
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

    velox's replacement for pytest's `pytester`: the tests that matter here are about what the
    *importer* does, so a real import through a real meta-path hook is the cheapest honest
    harness. Everything is torn down — hook, `sys.path`, `sys.modules` — because the hook is
    process-global state.
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
