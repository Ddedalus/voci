"""Per-test assertion state, held in ContextVars.

The vendored explanation engine reads three values while building a failure message: the custom
comparison hook, the assertion-pass hook, and the config controlling verbosity and truncation.
Each is a `ContextVar` here, so a value set for one test is invisible to its siblings.

`voci/_assertions/_vendor/util.py` exposes all three under their upstream names through a
module-level `__getattr__`, which is how the vendored code reaches them unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from voci._assertions._vendor._shim import Config

__all__ = ["CONTEXT_GLOBALS", "assertion_state", "get_config", "set_config"]

#: Called as `(op, left, right) -> str | None` to override the explanation for a comparison.
#: This is voci's replacement for pytest's `pytest_assertrepr_compare` hook.
_reprcompare: ContextVar[Callable[[str, object, object], str | None] | None] = ContextVar(
    "voci_reprcompare", default=None
)

#: Called as `(lineno, orig, expl)` for every *passing* assert, when the rewriter was built
#: with the assertion-pass hook enabled. Off by default — it changes codegen.
_assertion_pass: ContextVar[Callable[[int, str, str], None] | None] = ContextVar(
    "voci_assertion_pass", default=None
)

#: Drives verbosity and truncation limits while an explanation is being built.
_config: ContextVar[Config | None] = ContextVar("voci_assertion_config", default=None)

#: The mapping the vendored `util` module resolves attribute reads against. Keys match the
#: upstream global names, which is what makes the vendored code work unmodified.
CONTEXT_GLOBALS: dict[str, ContextVar[Any]] = {
    "_reprcompare": _reprcompare,
    "_assertion_pass": _assertion_pass,
    "_config": _config,
}


def get_config() -> Config | None:
    """The config driving assertion output in the current context."""
    return _config.get()


def set_config(config: Config | None) -> Token[Config | None]:
    """Set the config for the current context. Prefer `assertion_state` where it fits.

    Returns the `Token` so the set is reversible (`_config.reset(token)`) — the only thing that
    otherwise stands between a caller of this escape hatch and a context it can never restore.
    Callers who don't need it can simply ignore the return value.
    """
    return _config.set(config)


#: Sentinel distinguishing "argument not passed" from "explicitly passed `None`" in
#: `assertion_state`'s signature. A `None` default could not make that distinction, and nested
#: blocks — the case this context manager exists for — need it: "not passed" must inherit the
#: enclosing value, while `None` must clear it.
class _Keep:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<KEEP>"


_KEEP: Final[Any] = _Keep()


@contextmanager
def assertion_state(
    *,
    config: Config | None = _KEEP,
    reprcompare: Callable[[str, object, object], str | None] | None = _KEEP,
    assertion_pass: Callable[[int, str, str], None] | None = _KEEP,
) -> Iterator[None]:
    """Bind assertion state for the duration of the block, then restore it.

    Restoring is belt-and-braces: when each test runs in its own task the context is already
    copied, so the values cannot leak sideways. It matters for the synchronous case — nested
    blocks, and setup code that runs in the caller's own context.

    Each parameter defaults to a private sentinel rather than `None`, so a nested
    `assertion_state(reprcompare=f)` leaves the enclosing `config` and `assertion_pass` alone
    instead of silently clearing them. Pass `None` explicitly to clear one.
    """
    tokens: list[tuple[ContextVar[Any], Token[Any]]] = []
    if config is not _KEEP:
        tokens.append((_config, _config.set(config)))
    if reprcompare is not _KEEP:
        tokens.append((_reprcompare, _reprcompare.set(reprcompare)))
    if assertion_pass is not _KEEP:
        tokens.append((_assertion_pass, _assertion_pass.set(assertion_pass)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
