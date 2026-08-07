"""Per-test assertion state, held in ContextVars.

The vendored explanation engine reaches for three values while building a failure message:
the custom comparison hook, the assertion-pass hook, and the config that controls verbosity
and truncation. Upstream they are module globals that pytest saves and restores around each
test item. velox runs tests as concurrent asyncio tasks in one process, so a module global is
simply wrong — this is the one genuine concurrency blocker in the whole subsystem (spec/07
§4.1), and ContextVars are the entire fix: asyncio copies the context into each task, so a
value set for one test is invisible to its siblings.

`velox/_vendor/assertion/util.py` exposes these under their upstream names via a module-level
`__getattr__`, so the vendored rewriter's `util._reprcompare` lookups never had to change.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velox._vendor.assertion._shim import Config

__all__ = ["CONTEXT_GLOBALS", "assertion_state", "get_config", "set_config"]

#: Called as `(op, left, right) -> str | None` to override the explanation for a comparison.
#: This is velox's replacement for pytest's `pytest_assertrepr_compare` hook (spec/07 §9).
_reprcompare: ContextVar[Callable[[str, object, object], str | None] | None] = ContextVar(
    "velox_reprcompare", default=None
)

#: Called as `(lineno, orig, expl)` for every *passing* assert, when the rewriter was built
#: with the assertion-pass hook enabled. Off by default — it changes codegen.
_assertion_pass: ContextVar[Callable[[int, str, str], None] | None] = ContextVar(
    "velox_assertion_pass", default=None
)

#: Drives verbosity and truncation limits while an explanation is being built.
_config: ContextVar[Config | None] = ContextVar("velox_assertion_config", default=None)

#: The mapping the vendored `util` module resolves attribute reads against. Keys are the
#: upstream global names, deliberately: that is what makes the vendored code work unmodified.
CONTEXT_GLOBALS: dict[str, ContextVar[Any]] = {
    "_reprcompare": _reprcompare,
    "_assertion_pass": _assertion_pass,
    "_config": _config,
}


def get_config() -> Config | None:
    """The config driving assertion output in the current context."""
    return _config.get()


def set_config(config: Config | None) -> None:
    """Set the config for the current context. Prefer `assertion_state` where it fits."""
    _config.set(config)


@contextmanager
def assertion_state(
    *,
    config: Config | None = None,
    reprcompare: Callable[[str, object, object], str | None] | None = None,
    assertion_pass: Callable[[int, str, str], None] | None = None,
) -> Iterator[None]:
    """Bind assertion state for the duration of the block, then restore it.

    Restoring is belt-and-braces: when each test runs in its own task the context is already
    copied, so the values cannot leak sideways. It matters for the synchronous case — nested
    blocks, and setup code that runs in the caller's own context.
    """
    config_token = _config.set(config)
    reprcompare_token = _reprcompare.set(reprcompare)
    assertion_pass_token = _assertion_pass.set(assertion_pass)
    try:
        yield
    finally:
        _config.reset(config_token)
        _reprcompare.reset(reprcompare_token)
        _assertion_pass.reset(assertion_pass_token)
