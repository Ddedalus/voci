"""Marks: decorators that attach one frozen record to the decorated function.

Marks are objects, not strings: `skip`, `skipif`, `xfail`, `parametrize`, `tag`, `timeout`,
`solo` and `isolated` each fold one frozen record into the function's `__velox_marks__`. Every
decorator returns the *same* function object with that attribute replaced, so stacking is
order-independent except for `parametrize` (see below).
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
    # `fn.__dict__.get(...)`, not `getattr`: getattr walks the MRO, so a subclass would inherit
    # its base's marks and `_amend` on the subclass would silently rewrite the *derived* record
    # from the inherited one. Not every object has a `__dict__` (slots, builtins), hence the
    # `getattr` guard around it — this must never raise.
    marks_dict = getattr(fn, "__dict__", None)
    if marks_dict is None:
        return Marks()
    return marks_dict.get(MARKS_ATTR) or Marks()


# Marks live on the function object (`__velox_marks__` is part of the tested surface — see
# `marks_of`), so a helper reused as a test body in two modules would otherwise accumulate both
# sites' marks, and a second `@skip` would silently overwrite the first. Accumulating marks
# (`skipifs`, `tags`, `parametrizations`) legitimately stack across decorators; a *scalar* mark
# applied twice to the same object is almost always a mistake — a duplicate `@skip`/`@xfail`/
# `@timeout` with two different reasons/timeouts has no sensible "last one wins" reading — so
# that case raises instead of overwriting silently.
_SCALAR_MARKS = frozenset({"skip", "xfail", "timeout"})


def _amend[F: Callable[..., Any]](fn: F, **changes: Any) -> F:
    current = marks_of(fn)
    for field, value in changes.items():
        if field in _SCALAR_MARKS and getattr(current, field) is not None:
            name = getattr(fn, "__name__", repr(fn))
            raise TypeError(
                f"@velox.{field} applied twice to {name!r}: already set to "
                f"{getattr(current, field)!r}, now {value!r}"
            )
    setattr(fn, MARKS_ATTR, dataclasses.replace(current, **changes))
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
    """Record an expected-failure mark, with `reason`. `strict` and `raises` refine what counts
    as the expected failure. Not yet enforced in reporting; see `ROADMAP.md`."""

    def decorate(fn: F) -> F:
        return _amend(fn, xfail=XFail(reason, strict=strict, raises=raises))

    return decorate


def tag[F: Callable[..., Any]](*names: str) -> Callable[[F], F]:
    """Attach selection tags to a test, exposed as `TestInfo.tags`. Not yet selectable via `-m`;
    see `ROADMAP.md`."""

    def decorate(fn: F) -> F:
        return _amend(fn, tags=(*marks_of(fn).tags, *names))

    return decorate


def timeout[F: Callable[..., Any]](seconds: float) -> Callable[[F], F]:
    """Record a per-test timeout, in seconds.

    Not yet enforced — every test is held to the suite-wide `--timeout` budget; see `ROADMAP.md`.
    """

    def decorate(fn: F) -> F:
        return _amend(fn, timeout=seconds)

    return decorate


def solo[F: Callable[..., Any]](fn: F) -> F:
    """Mark this test to run alone, with nothing else scheduled alongside it.

    Applied bare, with no parentheses. Not yet enforced; see `ROADMAP.md`.
    """
    return _amend(fn, solo=True)


def isolated[F: Callable[..., Any]](fn: F) -> F:
    """Mark this test to run in a subprocess on a fresh loop.

    Applied bare, with no parentheses. Not yet enforced; see `ROADMAP.md`.
    """
    return _amend(fn, isolated=True)


def parametrize[F: Callable[..., Any]](
    argnames: str | Sequence[str],
    argvalues: Sequence[object],
    *,
    ids: Sequence[str] | Callable[[object], str | None] | None = None,
) -> Callable[[F], F]:
    """Record a `parametrize` mark. `argnames` is `"a,b"` or `["a", "b"]`; with one name, each
    entry of `argvalues` is that value, with several, each entry is a tuple aligned to the
    names. Stacked decorators combine in a stable, defined order: outermost varies slowest.

    Not yet expanded at collection; see `ROADMAP.md`.
    """
    # Validated here, at decoration time: failing in the collector instead points the traceback
    # elsewhere, and a stale `ids` list would silently mislabel every later case instead of
    # raising.
    names = _split(argnames)
    if not names:
        raise ValueError(f"parametrize({argnames!r}): no argument names given")
    if len(set(names)) != len(names):
        raise ValueError(f"parametrize({argnames!r}): duplicate argument name")
    cases = tuple(_case(v, len(names)) for v in argvalues)
    for case in cases:
        if len(case) != len(names):
            raise ValueError(
                f"parametrize({argnames!r}): expected {len(names)} value(s) per case, "
                f"got {len(case)}: {case!r}"
            )
    fixed_ids = ids if ids is None or callable(ids) else tuple(ids)
    if isinstance(fixed_ids, tuple) and len(fixed_ids) != len(cases):
        raise ValueError(
            f"parametrize({argnames!r}): {len(fixed_ids)} id(s) for {len(cases)} case(s)"
        )
    param_set = ParamSet(names, cases, fixed_ids)

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
