"""`raises`: a context manager that catches an expected exception and exposes it as
`ExceptionInfo`, optionally matching the exception type and a regex against its message.

Given a second positional argument, `raises` calls it instead of being entered by a `with`:
`raises(expected, func, *args, **kwargs)` calls `func(*args, **kwargs)` and returns the same
`ExceptionInfo`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from types import TracebackType
from typing import Any, final, overload

__all__ = ["ExceptionInfo", "RaisesContext", "raises"]

type ExcTypes = type[BaseException] | tuple[type[BaseException], ...]


@final
class ExceptionInfo[E: BaseException]:
    """Handle on the exception a `raises` block caught. Populated on block exit."""

    __slots__ = ("_exc",)

    def __init__(self) -> None:
        self._exc: E | None = None

    @property
    def value(self) -> E:
        # RuntimeError, not AttributeError: AttributeError from a property is swallowed by
        # `hasattr`, `getattr(info, "value", default)`, and most repr/debug machinery, so a test
        # that reads `.value` too early would see a silent default instead of this error.
        if self._exc is None:
            raise RuntimeError("the raises() block has not completed yet")
        return self._exc

    @property
    def type(self) -> type[E]:
        return type(self.value)

    @property
    def traceback(self) -> TracebackType | None:
        return self.value.__traceback__

    def match(self, pattern: str | re.Pattern[str]) -> bool:
        """`re.search` the string form of the exception. Returns True or raises AssertionError."""
        if re.search(pattern, str(self.value)) is None:
            raise AssertionError(f"pattern {pattern!r} does not match {str(self.value)!r}")
        return True

    def __repr__(self) -> str:
        return f"<ExceptionInfo {self._exc!r}>"


@final
class RaisesContext[E: BaseException]:
    """Context manager returned by `raises`."""

    __slots__ = ("_expected", "_info", "_match")

    def __init__(
        self, expected: type[E] | tuple[type[E], ...], match: str | re.Pattern[str] | None
    ) -> None:
        # `expected` is bounded by BaseException, so without this check `raises(SystemExit)`'s
        # sibling `raises(asyncio.CancelledError)` (or `raises(BaseException)`) would swallow the
        # cancellation velox's own timeout machinery uses to stop a runaway test, making that test
        # un-timeout-able. SystemExit and KeyboardInterrupt are unaffected — testing a CLI's
        # SystemExit is legitimate and common.
        types = expected if isinstance(expected, tuple) else (expected,)
        if any(issubclass(asyncio.CancelledError, t) for t in types):
            raise TypeError(
                "raises() cannot catch asyncio.CancelledError: velox uses cancellation to "
                "enforce test timeouts, and a raises() block that swallows it would make that "
                "test un-timeout-able."
            )
        self._expected = expected
        self._match = match
        self._info: ExceptionInfo[E] = ExceptionInfo()

    def __enter__(self) -> ExceptionInfo[E]:
        return self._info

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object
    ) -> bool:
        __tracebackhide__ = True
        if exc is None:
            expected = getattr(self._expected, "__name__", repr(self._expected))
            raise AssertionError(f"DID NOT RAISE {expected}")
        if not isinstance(exc, self._expected):
            return False
        self._info._exc = exc
        if self._match is not None:
            self._info.match(self._match)
        return True


@overload
def raises[E: BaseException](
    expected: type[E] | tuple[type[E], ...],
    *,
    match: str | re.Pattern[str] | None = None,
) -> RaisesContext[E]: ...
@overload
def raises[E: BaseException](
    expected: type[E] | tuple[type[E], ...],
    func: Callable[..., object],
    *args: Any,
    match: str | re.Pattern[str] | None = None,
    **kwargs: Any,
) -> ExceptionInfo[E]: ...
def raises(expected, func=None, *args, match=None, **kwargs):
    """Assert that `expected` is raised, optionally with a message matching `match`.

    Used as a context manager, `raises` returns a `RaisesContext` whose `__enter__` hands back
    an `ExceptionInfo`, populated once the block exits. Given a second positional argument, it
    instead calls `func(*args, **kwargs)` under the same machinery and returns the
    `ExceptionInfo` directly; a non-callable `func` raises `TypeError`.

    `match` always matches against the raised exception, in both forms — it is never one of
    `func`'s `**kwargs`, so a call means the same thing regardless of which form invoked it.

    `match` is an `re.search`, not a full match — pytest-compatible, including the gotcha that
    regex metacharacters in a literal message need escaping.
    """
    if func is None:
        if args or kwargs:
            raise TypeError("raises() got positional/keyword arguments without a callable `func`")
        return RaisesContext(expected, match)
    if not callable(func):
        raise TypeError(f"{func!r} is not callable")
    with RaisesContext(expected, match) as info:
        func(*args, **kwargs)
    return info
