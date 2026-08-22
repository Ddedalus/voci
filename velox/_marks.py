"""Marks: decorators that attach one frozen record to the decorated function.

Marks are objects, not strings: `skip`, `skipif`, `xfail`, `parametrize`, `tag`, `timeout`,
`solo` and `isolated` each fold one frozen record into the function's `__velox_marks__`. Every
decorator returns the *same* function object with that attribute replaced, so stacking is
order-independent except for `parametrize` (see below).
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "NO_MARKS",
    "MarkDecorator",
    "Marks",
    "ParamCase",
    "ParamSet",
    "Skip",
    "SkipIf",
    "XFail",
    "case",
    "decided",
    "holds",
    "isolated",
    "marks_of",
    "merged",
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

type MarkDecorator = Callable[[Any], Any]
"""What `case(marks=...)` takes: `velox.skip("...")`, `velox.solo`, or any other decorator that
folds a mark into the function it is handed."""


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
    """An expected failure, `condition` deciding whether it is expected at all.

    `condition` is read the way `SkipIf`'s is -- a `bool`, or a zero-argument callable for one
    that must not be computed during import -- and decided once, at collection. A false one
    leaves the test's own pass or fail standing.
    """

    reason: str
    strict: bool = False
    raises: ExcTypes | None = None
    condition: bool | Callable[[], bool] = True


@dataclass(frozen=True, slots=True)
class ParamSet:
    """One `@parametrize` application, normalized.

    `argvalues` is always a tuple of tuples, one inner tuple per case, aligned with `argnames`.
    """

    argnames: tuple[str, ...]
    argvalues: tuple[tuple[object, ...], ...]
    ids: tuple[str, ...] | Callable[[object], str | None] | None = None
    case_marks: tuple[Marks, ...] = ()
    """One entry per case in `argvalues`, read off the `case(...)` wrappers among them -- or
    empty, the common case, when no case carries marks of its own."""


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


NO_MARKS = Marks()
"""The empty record, shared: what a function with no marks and a case with no marks of its own
both have, and the default for anything that carries a `Marks` field."""


@dataclass(frozen=True, slots=True)
class ParamCase:
    """One `@parametrize` case with marks of its own, as `case(...)` builds it."""

    values: tuple[object, ...]
    marks: Marks


def marks_of(fn: object) -> Marks:
    """The marks attached to `fn`, or an empty record. Never raises."""
    # `fn.__dict__.get(...)`, not `getattr`: getattr walks the MRO, so a subclass would inherit
    # its base's marks and `_amend` on the subclass would silently rewrite the *derived* record
    # from the inherited one. Not every object has a `__dict__` (slots, builtins), hence the
    # `getattr` guard around it — this must never raise.
    marks_dict = getattr(fn, "__dict__", None)
    if marks_dict is None:
        return NO_MARKS
    return marks_dict.get(MARKS_ATTR) or NO_MARKS


def merged(base: Marks, extra: Marks) -> Marks:
    """`base` with `extra` folded into it -- how one case's own marks reach that case's test.

    The specific one wins where only one can: `extra`'s `skip`, `xfail` and `timeout` stand in
    for `base`'s. The accumulating marks accumulate, as they do across stacked decorators, and
    `parametrizations` are `base`'s alone -- a case parametrizes nothing.
    """
    if extra == NO_MARKS:
        return base
    return Marks(
        skip=extra.skip or base.skip,
        skipifs=(*base.skipifs, *extra.skipifs),
        xfail=extra.xfail or base.xfail,
        tags=(*base.tags, *extra.tags),
        timeout=base.timeout if extra.timeout is None else extra.timeout,
        solo=base.solo or extra.solo,
        isolated=base.isolated or extra.isolated,
        parametrizations=base.parametrizations,
    )


def holds(condition: bool | Callable[[], bool]) -> bool:
    """Whether a `SkipIf`/`XFail` condition is true, calling it if that is what it is."""
    return bool(condition() if callable(condition) else condition)


def decided(marks: Marks) -> Marks:
    """`marks` with its `xfail` condition decided, so nothing downstream evaluates it again.

    An expectation that does not hold is dropped outright rather than carried as a false one:
    whoever reads the record then has the one question they ask -- is there an `xfail` --
    already answered. `skipif` needs no such pass: its answer is a reason, which
    `_collection.collect` reads for itself.
    """
    if marks.xfail is None or holds(marks.xfail.condition):
        return marks
    return dataclasses.replace(marks, xfail=None)


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
    reason: str,
    *,
    condition: bool | Callable[[], bool] = True,
    strict: bool = False,
    raises: ExcTypes | None = None,
) -> Callable[[F], F]:
    """Record an expected-failure mark, with `reason`. A call phase that raises reports
    `XFAILED` instead of `FAILED`; one that passes reports `XPASSED`, or fails the test outright
    if `strict` is set. `raises`, if given, narrows which exception type counts as the expected
    failure -- any other exception still reports `FAILED`. `condition` expects the failure only
    where it holds, read the way `skipif`'s is: a `bool`, or a zero-argument callable for one
    that must not be computed during import."""

    def decorate(fn: F) -> F:
        return _amend(fn, xfail=XFail(reason, strict=strict, raises=raises, condition=condition))

    return decorate


def tag[F: Callable[..., Any]](*names: str) -> Callable[[F], F]:
    """Attach selection tags to a test, exposed as `TestInfo.tags` and selectable with `-m`."""

    def decorate(fn: F) -> F:
        return _amend(fn, tags=(*marks_of(fn).tags, *names))

    return decorate


def timeout[F: Callable[..., Any]](seconds: float) -> Callable[[F], F]:
    """Record a per-test timeout, in seconds, overriding the suite-wide `--timeout` budget for
    this test alone."""

    def decorate(fn: F) -> F:
        if not (math.isfinite(seconds) and seconds > 0):
            raise ValueError(f"timeout must be a positive, finite number of seconds, got {seconds}")
        return _amend(fn, timeout=seconds)

    return decorate


def solo[F: Callable[..., Any]](fn: F) -> F:
    """Mark this test to run alone, with nothing else scheduled alongside it.

    Applied bare, with no parentheses. `_run.AdmissionGate` admits it only once nothing else is
    running, and blocks every other test's admission until it finishes.
    """
    return _amend(fn, solo=True)


def isolated[F: Callable[..., Any]](fn: F) -> F:
    """Mark this test to run alone in a subprocess, on its own fresh interpreter and loop.

    Applied bare, with no parentheses. Still admitted through the same concurrency/`exclusive=`/
    `solo` gate as every other test (`_run.AdmissionGate`) -- the subprocess is what's fresh, not
    the scheduling. A module-scope fixture this test shares with in-process siblings is set up
    and torn down separately inside the subprocess, not shared with them.
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
    An entry written as `case(value, marks=...)` carries marks for that one case, folded into
    the test's own for the record that case expands into. Expanded into one test per case at
    collection (`_collection.parametrize.cases_for`).
    """
    # Validated here, at decoration time: failing in the collector instead points the traceback
    # elsewhere, and a stale `ids` list would silently mislabel every later case instead of
    # raising.
    names = _split(argnames)
    if not names:
        raise ValueError(f"parametrize({argnames!r}): no argument names given")
    if len(set(names)) != len(names):
        raise ValueError(f"parametrize({argnames!r}): duplicate argument name")
    cases = tuple(_case_values(v, len(names)) for v in argvalues)
    per_case = tuple(v.marks if isinstance(v, ParamCase) else NO_MARKS for v in argvalues)
    if not cases:
        # Caught here rather than left to expand into zero records: a `@parametrize` that
        # contributes no cases would otherwise make the test vanish from the suite with no
        # `CollectionError`, no `Skipped` entry, and no count discrepancy visible at a glance.
        raise ValueError(f"parametrize({argnames!r}): no argvalues given")
    for case_values in cases:
        if len(case_values) != len(names):
            raise ValueError(
                f"parametrize({argnames!r}): expected {len(names)} value(s) per case, "
                f"got {len(case_values)}: {case_values!r}"
            )
    fixed_ids = ids if ids is None or callable(ids) else tuple(ids)
    if isinstance(fixed_ids, tuple) and len(fixed_ids) != len(cases):
        raise ValueError(
            f"parametrize({argnames!r}): {len(fixed_ids)} id(s) for {len(cases)} case(s)"
        )
    # Left empty unless some case actually carries marks: `case_marks` is then an alignment
    # every reader of a `ParamSet` would have to keep in step for nothing.
    marked = per_case if any(case_marks != NO_MARKS for case_marks in per_case) else ()
    param_set = ParamSet(names, cases, fixed_ids, marked)

    def decorate(fn: F) -> F:
        # Prepend: decorators apply bottom-up, so the last one applied is the outermost, and
        # outermost must end up first — slowest-varying under `product()`.
        return _amend(fn, parametrizations=(param_set, *marks_of(fn).parametrizations))

    return decorate


def case(*values: object, marks: MarkDecorator | Sequence[MarkDecorator] = ()) -> ParamCase:
    """One `@parametrize` case, carrying marks that reach that case alone.

    `marks` are the very decorators a test carries -- `case(2, marks=velox.skip("flaky"))`,
    `case(3, marks=[velox.xfail("known"), velox.tag("slow")])` -- and mean for this case what
    they would mean written above the `def`. Every mark but `parametrize` works this way; the
    test's own marks and the case's are folded together, the case's winning where only one of
    `skip`, `xfail` or `timeout` can stand.

    `values` are that case's values, one per argname, exactly as they would be written without
    the wrapper: `case(1, 2)` for two argnames, `case((1, 2))` for one that takes a tuple.
    """
    decorators = [marks] if callable(marks) else list(marks)

    def marked() -> None:
        """Stand-in for the test this case's marks are being read for."""

    # Marks are read by applying the decorators to a stand-in function, rather than by naming
    # the record types here: one spelling for a case's marks and a test's, and the decorators'
    # own validation -- a duplicate scalar, a non-positive timeout -- is what rejects a
    # malformed case, at decoration time, where the traceback points at the case list.
    marked.__name__ = "velox.case(...)"
    for decorator in decorators:
        if decorator(marked) is not marked:
            raise TypeError(f"case(marks=...): {decorator!r} is not a velox mark decorator")
    collected = marks_of(marked)
    if decorators and collected == NO_MARKS:
        # A `pytest.mark.*` decorator, say: it takes a function and hands the same one back,
        # marking it for a runner that is not this one, and would otherwise be silently dropped.
        raise TypeError(f"case(marks=...): {marks!r} attached no velox marks")
    if collected.parametrizations:
        raise TypeError("case(marks=...): @velox.parametrize applies to a whole test, not a case")
    return ParamCase(values, collected)


def _split(argnames: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(argnames, str):
        return tuple(n.strip() for n in argnames.split(",") if n.strip())
    return tuple(argnames)


def _case_values(value: object, arity: int) -> tuple[object, ...]:
    if isinstance(value, ParamCase):
        # Already one value per argname, however many that is: `case(1, 2)` wrote them out, and
        # `case((1, 2))` for a single argname wrote one value that happens to be a tuple. The
        # arity check in `parametrize` is what rejects a case that named the wrong number.
        return value.values
    if arity == 1:
        return (value,)
    return tuple(value) if isinstance(value, tuple | list) else (value,)
