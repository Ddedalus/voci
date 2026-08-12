"""Expanding `@velox.parametrize` into one callspec per case.

`known_params_of` and `cases_for` both take a test's `Marks.parametrizations` tuple — outermost
first, per `_marks.parametrize`'s stacking order — and are the two things `collect.py` needs: which
argument names parametrize supplies (so `_di.plan_for` stops treating them as missing
`Depends(...)` sites), and the call kwargs plus display id for each expanded case.
"""

from __future__ import annotations

import enum
import itertools
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from velox._marks import ParamSet

__all__ = ["Case", "cases_for", "known_params_of"]


@dataclass(frozen=True, slots=True)
class Case:
    """One expanded `@velox.parametrize` callspec: its call kwargs and display id."""

    params: dict[str, object]
    id: str


def known_params_of(parametrizations: tuple[ParamSet, ...]) -> frozenset[str]:
    """The union of every stacked `@parametrize`'s argnames.

    Raises `ValueError` if two stacked applications declare the same name — left unchecked, the
    second would silently overwrite the first's value in every expanded case's kwargs.
    """
    seen: set[str] = set()
    for param_set in parametrizations:
        for name in param_set.argnames:
            if name in seen:
                raise ValueError(
                    f"parametrize: argument name {name!r} is supplied by more than one stacked "
                    f"@velox.parametrize on the same test"
                )
            seen.add(name)
    return frozenset(seen)


def cases_for(parametrizations: tuple[ParamSet, ...]) -> tuple[Case, ...]:
    """The cartesian product of `parametrizations`, one `Case` per combination.

    Outermost varies slowest, matching `Marks.parametrizations`' own order — `itertools.product`
    varies its last argument fastest, so feeding the tuple through in that order is enough. Ids
    are generated per-value (str/int/bool/None/enum become their literal; anything else becomes
    `argname<index>`), joined with `-` across a case's names and again across stacked
    parametrizations, then disambiguated by appending an occurrence count to any id that collides
    with another after generation.
    """
    if not parametrizations:
        return ()

    per_set = [_case_options(param_set) for param_set in parametrizations]
    return _dedupe(_merge(combo) for combo in itertools.product(*per_set))


def _merge(combo: tuple[tuple[dict[str, object], str], ...]) -> Case:
    """One product tuple -- one `(values, case_id)` pair per stacked parametrization -- flattened
    into a single `Case`."""
    params: dict[str, object] = {}
    for values, _ in combo:
        params.update(values)
    return Case(params=params, id="-".join(case_id for _, case_id in combo))


def _case_options(param_set: ParamSet) -> tuple[tuple[dict[str, object], str], ...]:
    ids = _case_ids(param_set)
    return tuple(
        (dict(zip(param_set.argnames, values, strict=True)), case_id)
        for values, case_id in zip(param_set.argvalues, ids, strict=True)
    )


def _case_ids(param_set: ParamSet) -> tuple[str, ...]:
    if isinstance(param_set.ids, tuple):
        return param_set.ids
    idfn = param_set.ids if callable(param_set.ids) else None
    return tuple(
        "-".join(
            _value_id(value, argname, index, idfn)
            for argname, value in zip(param_set.argnames, case, strict=True)
        )
        for index, case in enumerate(param_set.argvalues)
    )


def _value_id(
    value: object, argname: str, index: int, idfn: Callable[[object], str | None] | None
) -> str:
    if idfn is not None:
        try:
            generated = idfn(value)
        except Exception:  # a broken id callable must not abort collection -- fall back instead
            generated = None
        if isinstance(generated, str):
            return generated
    return _auto_id(value, argname, index)


def _auto_id(value: object, argname: str, index: int) -> str:
    # `bool` before `int`: `isinstance(True, int)` is true, and `str(True)` ("True") is the
    # readable id -- landing in a combined `int`/`str`/`bool` branch first would give "1" instead.
    if isinstance(value, bool | str | int) or value is None:
        return str(value)
    if isinstance(value, enum.Enum):
        return str(value.name)
    return f"{argname}{index}"


def _dedupe(cases: Iterable[Case]) -> tuple[Case, ...]:
    """Disambiguate cases whose generated ids collide.

    Every id that occurs exactly once is reserved as-is, up front, before any renaming happens.
    Each case sharing a duplicated id then gets the lowest `f"{id}{n}"` not already reserved --
    checked against every id reserved so far, not just the ids in its own collision group, so a
    renamed id can never land on one either already unique or already claimed by an earlier
    rename. Without that cross-check, `@parametrize("x", [1, 1, "10"])` would rename its two `1`s
    to `10`/`11` and collide with the third case's already-unique `"10"`.
    """
    cases = tuple(cases)
    counts = Counter(case.id for case in cases)
    used = {id_ for id_, count in counts.items() if count == 1}
    next_occurrence: dict[str, int] = {}
    result: list[Case] = []
    for case in cases:
        if counts[case.id] == 1:
            result.append(case)
            continue
        occurrence = next_occurrence.get(case.id, 0)
        candidate = f"{case.id}{occurrence}"
        while candidate in used:
            occurrence += 1
            candidate = f"{case.id}{occurrence}"
        next_occurrence[case.id] = occurrence + 1
        used.add(candidate)
        result.append(Case(params=case.params, id=candidate))
    return tuple(result)
