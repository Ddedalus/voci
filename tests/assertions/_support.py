"""Plain helpers for the assertion tests: config/explanation builders, and the "write a tmp
module and import it, rewrite hook on or off" mechanics behind the rewritten/unrewritten
fixtures.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from voci._assertions import rewrite as _rewrite
from voci._assertions.rewrite import Config, explanation_lines


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
    """Ported from upstream `callop`; dispatches straight at voci's explanation entrypoint."""
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


@contextmanager
def imported_module(
    tmp_path: Path,
    *,
    rewrite: bool,
    cache_dir: Path | None = None,
) -> Iterator[Callable[..., Any]]:
    """Write-and-import a tmp module, with or without the rewrite hook installed. Yields a
    `build(source, name=...)` callable; tears down the hook (if installed), `sys.path`, and
    `sys.modules` on exit regardless of how the caller's block exits.
    """
    installed: list[str] = []
    roots = tmp_path / "suite"
    roots.mkdir(exist_ok=True)
    cache = cache_dir if cache_dir is not None else tmp_path / "cache"

    def build(source: str, *, name: str = "mod_under_test", **install_kwargs: Any) -> Any:
        path = roots / f"{name}.py"
        path.write_text(textwrap.dedent(source))
        if rewrite:
            _rewrite.install([roots], cache_dir=cache, **install_kwargs)
            sys.path.insert(0, str(roots))
            installed.append(name)
            return __import__(name)
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        installed.append(name)
        spec.loader.exec_module(module)
        return module

    try:
        yield build
    finally:
        if rewrite:
            _rewrite.uninstall()
        for name in installed:
            sys.modules.pop(name, None)
        while str(roots) in sys.path:
            sys.path.remove(str(roots))
