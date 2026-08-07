"""Marks: decorators that attach one frozen record to the decorated function.

Marks are objects, not strings. There is no open-ended mark namespace, no `--strict-markers`,
and no plugin-consumed marks — those exist in pytest to feed a hook system velox does not have.

Every decorator here returns the *same* function object with `__velox_marks__` replaced, so
stacking is order-independent except for `parametrize` (see below).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "Marks",
    "ParamSet",
    "Skip",
    "SkipIf",
    "XFail",
    "isolated",
    "marks_of",
    "parametrize",
    "skip",
    "skipif",
    "solo",
    "tag",
    "timeout",
    "xfail",
]

MARKS_ATTR = "__velox_marks__"

type ExcTypes = type[BaseException] | tuple[type[BaseException], ...]


@dataclass(frozen=True, slots=True)
class Skip:
    reason: str


@dataclass(frozen=True, slots=True)
class SkipIf:
    """A condition evaluated lazily, at run time, never at collection.

    A `bool` is of course already evaluated by the time it arrives; pass a zero-argument callable
    when the condition must not be computed during import.
    """

    condition: bool | Callable[[], bool]
    reason: str


@dataclass(frozen=True, slots=True)
class XFail:
    reason: str
    strict: bool = False
    raises: ExcTypes | None = None


@dataclass(frozen=True, slots=True)
class ParamSet:
    """One `@parametrize` application, normalized.

    `argvalues` is always a tuple of tuples, one inner tuple per case, aligned with `argnames`.
    """

    argnames: tuple[str, ...]
    argvalues: tuple[tuple[object, ...], ...]
    ids: tuple[str, ...] | Callable[[object], str | None] | None = None


@dataclass(frozen=True, slots=True)
class Marks:
    """Everything the collector needs to know about a test, read in one attribute lookup."""

    skip: Skip | None = None
    skipifs: tuple[SkipIf, ...] = ()
    xfail: XFail | None = None
    tags: tuple[str, ...] = ()
    timeout: float | None = None
    solo: bool = False
    isolated: bool = False
    parametrizations: tuple[ParamSet, ...] = ()
    """Outermost decorator first, which is also slowest-varying: feed straight to `product()`."""


def marks_of(fn: object) -> Marks:
    """The marks attached to `fn`, or an empty record. Never raises."""
    return getattr(fn, MARKS_ATTR, None) or Marks()


def _amend[F: Callable[..., Any]](fn: F, **changes: Any) -> F:
    setattr(fn, MARKS_ATTR, dataclasses.replace(marks_of(fn), **changes))
    return fn


def skip[F: Callable[..., Any]](reason: str) -> Callable[[F], F]:
    """Always skip this test."""

    def decorate(fn: F) -> F:
        return _amend(fn, skip=Skip(reason))

    return decorate


def skipif[F: Callable[..., Any]](
    condition: bool | Callable[[], bool], *, reason: str
) -> Callable[[F], F]:
    """Skip when `condition` holds. Stacks; any one truthy condition skips."""

    def decorate(fn: F) -> F:
        return _amend(fn, skipifs=(*marks_of(fn).skipifs, SkipIf(condition, reason)))

    return decorate


def xfail[F: Callable[..., Any]](
    reason: str, *, strict: bool = False, raises: ExcTypes | None = None
) -> Callable[[F], F]:
    """Expect failure. `strict=True` turns an unexpected pass into a failure; `raises=` narrows
    which exception counts as expected."""

    def decorate(fn: F) -> F:
        return _amend(fn, xfail=XFail(reason, strict=strict, raises=raises))

    return decorate


def tag[F: Callable[..., Any]](*names: str) -> Callable[[F], F]:
    """Attach selection tags, matched by `-m`. Replaces `@pytest.mark.<name>` for selection only."""

    def decorate(fn: F) -> F:
        return _amend(fn, tags=(*marks_of(fn).tags, *names))

    return decorate


def timeout[F: Callable[..., Any]](seconds: float) -> Callable[[F], F]:
    """Per-test timeout in seconds. Overrides `--timeout`."""

    def decorate(fn: F) -> F:
        return _amend(fn, timeout=seconds)

    return decorate


def solo[F: Callable[..., Any]](fn: F) -> F:
    """Run alone: the whole suite drains first, and nothing else runs alongside.

    Applied bare, with no parentheses. This is what a global write costs.
    """
    return _amend(fn, solo=True)


def isolated[F: Callable[..., Any]](fn: F) -> F:
    """Run in a subprocess on a fresh loop. Roadmap — accepted now, not yet honored.

    Applied bare, with no parentheses. Unlike `solo` this takes no suite-wide lock: an isolated
    test shares no process state, so it costs a spawn rather than the suite's concurrency.
    """
    return _amend(fn, isolated=True)


def parametrize[F: Callable[..., Any]](
    argnames: str | Sequence[str],
    argvalues: Sequence[object],
    *,
    ids: Sequence[str] | Callable[[object], str | None] | None = None,
) -> Callable[[F], F]:
    """Run the test once per case.

    `argnames` is `"a,b"` or `["a", "b"]`; with one name, each entry of `argvalues` is that value,
    with several, each entry is a tuple aligned to the names.

    Stacked decorators produce the cartesian product in a stable, defined order: outermost varies
    slowest. `indirect=` is not supported — the DI equivalent is a parametrized value passed into
    a fixture via `Fixture.with_()`.
    """
    names = _split(argnames)
    cases = tuple(_case(v, len(names)) for v in argvalues)
    for case in cases:
        if len(case) != len(names):
            raise ValueError(
                f"parametrize({argnames!r}): expected {len(names)} value(s) per case, "
                f"got {len(case)}: {case!r}"
            )
    param_set = ParamSet(names, cases, ids if ids is None or callable(ids) else tuple(ids))

    def decorate(fn: F) -> F:
        # Prepend: decorators apply bottom-up, so the last one applied is the outermost, and
        # outermost must end up first — slowest-varying under `product()`.
        return _amend(fn, parametrizations=(param_set, *marks_of(fn).parametrizations))

    return decorate


def _split(argnames: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(argnames, str):
        return tuple(n.strip() for n in argnames.split(",") if n.strip())
    return tuple(argnames)


def _case(value: object, arity: int) -> tuple[object, ...]:
    if arity == 1:
        return (value,)
    return tuple(value) if isinstance(value, tuple | list) else (value,)
