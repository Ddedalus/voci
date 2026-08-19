"""The cases a call site decided, and where velox reads each of them instead.

pytest lets somewhere other than the fixture decide what a test's cases are: an `indirect` mark
hands a fixture its values from the test, and a `pytest_generate_tests` hook builds whole axes
while the module is collected. velox has neither seam — a fixture carries its own `params=` and a
test carries its own `@velox.parametrize` — so both constructs convert by moving the case list to
the object that will read it.

Both lists are written from the dump rather than from the source that produced them. A hook's
cases have no source to copy in the first place, and an indirect mark's values are written in a
test module while the `params=` they become is read in whichever module holds the fixture, so
either way what travels is the value pytest passed, spelled as a literal. A value whose `repr` is
not a literal is a value this cannot write, and the case is refused.

Nothing here reads a fixture body or a test body. What it reads is the callspec of every case,
which is the same evidence the id-preserving `parametrize` rewrite already runs on.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto

import libcst as cst

from velox_migrate.audit import Finding, Site
from velox_migrate.model import CallSpec, FixtureDef, GroundTruth, Item

__all__ = [
    "Axis",
    "Carried",
    "Decision",
    "Generated",
    "axes",
    "decide",
    "ids_by_axis",
    "literal",
]


class Kind(Enum):
    """Who decided one axis's values, which is what says where velox has to read them."""

    MARK = auto()
    """A `@pytest.mark.parametrize` on the test: `VX101` already rewrites it."""

    FIXTURE = auto()
    """The fixture's own `params=`: velox spells it the same way, so nothing moves."""

    INDIRECT = auto()
    """A call site choosing a fixture's case: the values move onto the fixture."""

    GENERATED = auto()
    """A `pytest_generate_tests` hook: the cases become a parametrize listing them."""


@dataclass(frozen=True, slots=True)
class Axis:
    """One set of argnames that varies together across the cases of one test.

    `values` holds the `repr` of each argname's value, one row per case index in index order.
    `ids` is pytest's own id for each of those rows, and `position` where that id sits in the
    composed case id — both `None` where no single position of the id moves with this axis alone,
    which is the `VX114` case.
    """

    argnames: tuple[str, ...]
    values: tuple[tuple[str, ...], ...]
    ids: tuple[str, ...] | None
    position: int | None

    @property
    def key(self) -> str:
        """The axis as a rule spells it: the argnames joined the way the mark wrote them."""
        return ",".join(self.argnames)


@dataclass(frozen=True, slots=True)
class Carried:
    """The case list an indirect parametrization moves onto the fixture it chose cases for."""

    values: tuple[str, ...]
    ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Generated:
    """One hook-built axis, as the `@velox.parametrize` that lists what the hook produced."""

    argnames: tuple[str, ...]
    values: tuple[tuple[str, ...], ...]
    ids: tuple[str, ...] | None

    @property
    def key(self) -> str:
        return ",".join(self.argnames)


@dataclass(frozen=True, slots=True)
class Decision:
    """What this suite's call-site parametrizations become.

    `carried` is keyed by fixture key and `dropped` by the site of each test whose indirect mark
    the rewrite therefore takes away, as the axis keys it may drop. `generated` is keyed by site
    too, outermost axis first — velox reads its own list outermost-first, so writing them in the
    order pytest composed its ids in keeps every id where it was.
    """

    carried: Mapping[str, Carried]
    dropped: Mapping[tuple[str, str], frozenset[str]]
    generated: Mapping[tuple[str, str], tuple[Generated, ...]]
    findings: tuple[Finding, ...]


def decide(
    ground_truth: GroundTruth,
    *,
    items: Mapping[tuple[str, str], tuple[Item, ...]],
    translatable: Mapping[str, FixtureDef],
) -> Decision:
    """What each call-site parametrization in the suite becomes, and what refuses it.

    `items` are the suite's tests grouped by the site they are written at, every case of one test
    together, since an axis is a property of the case list rather than of any one case.
    """
    reading = _reading(ground_truth, items, translatable)
    return Decision(
        carried=reading.carried,
        dropped=reading.dropped,
        generated=reading.generated,
        findings=tuple(sorted(reading.findings, key=lambda f: (f.site.sort_key, f.code))),
    )


def axes(cases: Sequence[Item]) -> tuple[Axis, ...]:
    """Every axis the cases of one test vary over, or nothing if they are not one full product.

    Names that share an index in every case were parametrized together, so they are one axis, kept
    in the order the dump lists them — which is the order they were registered in and so the order
    the mark that covers them spells them.
    """
    specs = [item.callspec for item in cases if item.callspec is not None]
    if len(specs) != len(cases) or not specs:
        return ()
    found: list[Axis] = []
    for argnames in _grouped(specs):
        position, ids = _position_of(specs, argnames)
        found.append(
            Axis(
                argnames=argnames,
                values=_values(specs, argnames),
                ids=ids,
                position=position,
            )
        )
    return tuple(found)


def ids_by_axis(
    items: Mapping[tuple[str, str], tuple[Item, ...]], path: str
) -> Mapping[tuple[str, str], tuple[str, ...]]:
    """pytest's own ids for each axis in `path`, keyed by the test and the axis's argnames.

    A case id is composed from every axis that varies the test, so an id can only be written onto
    one `parametrize` mark where exactly one position of pytest's own id moves with that axis's
    index and stands still within it. Where no position does, the axis is `VX114` and the ids are
    velox's to generate.
    """
    found: dict[tuple[str, str], tuple[str, ...]] = {}
    for (file, qualname), cases in items.items():
        if file != path:
            continue
        for axis in axes(cases):
            if axis.ids is not None:
                found[(qualname, axis.key)] = axis.ids
    return found


def literal(text: str) -> cst.BaseExpression | None:
    """`text` — the `repr` of one case value — as an expression, or `None` if it is not one.

    A `repr` is source only for the literals `ast.literal_eval` reads back, so this is both the
    test of whether a value can be written down and the writing of it. What comes out is spelled
    canonically rather than as the `repr` was, so the case list reads like the rest of the file.
    """
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    try:
        return _expression(value)
    except (ValueError, cst.ParserSyntaxError):
        return None


def _expression(value: object) -> cst.BaseExpression:
    match value:
        case str():
            return cst.SimpleString(_quoted(value))
        case tuple():
            return cst.Tuple([cst.Element(_expression(item)) for item in value])
        case list():
            return cst.List([cst.Element(_expression(item)) for item in value])
        case set() | frozenset():
            # A set has no source order of its own, so its elements are written sorted by the way
            # they are spelled, which is the same list every time this runs.
            elements = sorted(_render(_expression(item)) for item in value)
            spelled = ", ".join(elements)
            return cst.parse_expression(f"{{{spelled}}}" if value else "set()")
        case dict():
            return cst.Dict(
                [
                    cst.DictElement(key=_expression(key), value=_expression(item))
                    for key, item in value.items()
                ]
            )
        case _:
            return cst.parse_expression(repr(value))


def _quoted(text: str) -> str:
    """`text` as a double-quoted literal, escaped the way every other rule writes a string."""
    return json.dumps(text, ensure_ascii=False)


def _render(node: cst.BaseExpression) -> str:
    return cst.Module(body=()).code_for_node(node)


# --- reading the axes of every test -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Use:
    """One test's indirect axis over one fixture, with every case the axis fans it into."""

    site: tuple[str, str]
    cases: tuple[Item, ...]
    axis: Axis
    alone: bool
    owned: bool

    @property
    def item(self) -> Item:
        return self.cases[0]


@dataclass(slots=True)
class _Reading:
    carried: dict[str, Carried]
    dropped: dict[tuple[str, str], frozenset[str]]
    generated: dict[tuple[str, str], tuple[Generated, ...]]
    findings: list[Finding]


def _reading(
    ground_truth: GroundTruth,
    items: Mapping[tuple[str, str], tuple[Item, ...]],
    translatable: Mapping[str, FixtureDef],
) -> _Reading:
    reading = _Reading(carried={}, dropped={}, generated={}, findings=[])
    uses: dict[str, list[_Use]] = {}
    for site, cases in sorted(items.items()):
        classified = [(axis, _kind_of(cases[0], axis)) for axis in axes(cases)]
        indirect = [axis for axis, kind in classified if kind is Kind.INDIRECT]
        own = sum(1 for _, kind in classified if kind in (Kind.INDIRECT, Kind.FIXTURE))
        for axis in indirect:
            found = cases[0].resolve(axis.argnames[0])
            if found is None:
                continue
            uses.setdefault(found.key, []).append(
                _Use(
                    site=site,
                    cases=tuple(cases),
                    axis=axis,
                    alone=own == 1,
                    owned=_own_mark(cases[0], axis.argnames[0]),
                )
            )
        _read_generated(reading, site, cases, classified)
    for key, found in sorted(uses.items()):
        _read_indirect(reading, ground_truth, translatable, key, found)
    return reading


def _read_indirect(
    reading: _Reading,
    ground_truth: GroundTruth,
    translatable: Mapping[str, FixtureDef],
    key: str,
    uses: Sequence[_Use],
) -> None:
    """Whether the fixture `key` can carry the values its call sites gave it, and what if not."""
    fixture = ground_truth.fixture_defs[key]
    reason = _uncarriable(ground_truth, translatable, fixture, uses)
    if reason is not None:
        reading.findings.extend(_refusals(fixture, uses, reason))
        return
    first = uses[0].axis
    assert first.ids is not None
    reading.carried[key] = Carried(values=tuple(row[0] for row in first.values), ids=first.ids)
    for use in uses:
        site = use.site
        reading.dropped[site] = reading.dropped.get(site, frozenset()) | {use.axis.key}


def _uncarriable(
    ground_truth: GroundTruth,
    translatable: Mapping[str, FixtureDef],
    fixture: FixtureDef,
    uses: Sequence[_Use],
) -> str | None:
    """Why this fixture cannot take its call sites' values as `params=`, or `None` if it can.

    A `params=` fixture has one case list and every test that reaches it runs once per case, so
    what has to hold is that the fixture is one object, that the object has no cases of its own,
    and that every test reaching it asked for the same ones in a shape velox composes its ids the
    same way from.
    """
    name = fixture.argname
    if fixture.key not in translatable:
        return f"`{name}` is not a fixture this conversion writes an object for"
    if len(ground_truth.fixture_registry.get(name, ())) != 1:
        return f"this suite defines `{name}` in more than one place"
    if fixture.is_parametrized:
        return f"`{name}` already has cases of its own"
    if fixture.autouse:
        return f"`{name}` is autouse, so no test names the case it wants"
    first = uses[0].axis
    if len(first.argnames) != 1:
        return f"`{name}` is parametrized together with `{'`, `'.join(first.argnames[1:])}`"
    for use in uses:
        if use.axis.values != first.values or use.axis.ids != first.ids:
            return f"`{name}` is given different values by different tests"
        if use.axis.ids is None or use.axis.position != 0:
            return f"the ids of `{name}`'s cases are composed with another axis of the same test"
        if not use.alone:
            return f"`{name}` shares its test with another parametrized fixture"
        if not use.owned:
            return (
                f"the values reach `{name}` from somewhere other than the test's own decorator, "
                "which is the only place a rewrite takes a mark away from"
            )
    if any(literal(row[0]) is None for row in first.values):
        return f"a value `{name}` is given has no literal spelling"
    reached = {
        item.nodeid
        for item in ground_truth.items
        if any(found.key == fixture.key for found in item.walk())
    }
    parametrized = {item.nodeid for use in uses for item in use.cases}
    if reached - parametrized:
        return f"tests that reach `{name}` without parametrizing it would gain its cases"
    return None


def _refusals(fixture: FixtureDef, uses: Sequence[_Use], reason: str) -> Iterator[Finding]:
    """One `VX029` per test whose indirect mark stays where it was written.

    Sited on the test rather than on the fixture: the mark is what velox has no answer for, the
    fixture is often fine for every other test that reaches it, and the test is what a reader has
    to decide about.
    """
    for use in uses:
        yield Finding(
            code="VX029",
            message=(
                f"`{use.item.originalname}` parametrizes `{fixture.argname}` indirectly, and "
                f"{reason}."
            ),
            site=Site(use.item.path, use.item.lineno, _qualname_of(use.item)),
            tests=tuple(sorted(case.nodeid for case in use.cases)),
            detail={"fixture": fixture.argname, "axis": use.axis.key},
        )


def _read_generated(
    reading: _Reading,
    site: tuple[str, str],
    cases: Sequence[Item],
    classified: Sequence[tuple[Axis, Kind]],
) -> None:
    """The parametrize decorators one test's hook-built axes become, or the refusal instead.

    Every axis of the test has to be one the hook built. An axis written as a mark is one the
    `parametrize` rule already reverses into place, and a generated decorator among those would
    have to take a position in that stack that nothing has decided.
    """
    hooked = [axis for axis, kind in classified if kind is Kind.GENERATED]
    if not hooked:
        return
    item = cases[0]
    reason = _unlistable(classified, hooked)
    if reason is not None:
        reading.findings.append(
            Finding(
                code="VX031",
                message=f"`{item.originalname}` takes cases a hook produced, and {reason}.",
                site=Site(item.path, item.lineno, _qualname_of(item)),
                tests=tuple(sorted(case.nodeid for case in cases)),
                detail={"axes": len(hooked)},
            )
        )
        return
    ordered = sorted(hooked, key=lambda axis: (axis.position is None, axis.position or 0))
    reading.generated[site] = tuple(
        Generated(argnames=axis.argnames, values=axis.values, ids=axis.ids) for axis in ordered
    )


def _unlistable(classified: Sequence[tuple[Axis, Kind]], hooked: Sequence[Axis]) -> str | None:
    others = [axis for axis, kind in classified if kind is not Kind.GENERATED]
    if others:
        spelled = "`, `".join(axis.key for axis in others)
        return f"its cases are composed with `{spelled}`, which the test names itself"
    for axis in hooked:
        for row in axis.values:
            for value in row:
                if literal(value) is None:
                    return f"`{value}` is a value with no literal spelling"
    return None


def _own_mark(item: Item, name: str) -> bool:
    """Whether the `indirect` mark covering `name` is written on this test's own `def`.

    A mark on a class, in a `pytestmark`, or no mark at all — a hook calling
    `metafunc.parametrize(..., indirect=True)` — leaves the rewrite with no decorator to take
    away, and a `params=` written while the mark stays would parametrize the fixture twice over.
    """
    for mark in item.markers_with_origin:
        if mark.name == "parametrize" and mark.args and name in _named(mark.args[0]):
            return mark.origin == item.nodeid
    return False


def _kind_of(item: Item, axis: Axis) -> Kind:
    """Who decided this axis's values.

    A mark says so itself, which is what tells an indirect axis from the cases a fixture carries
    under the same name. Where no mark covers the axis, what the name resolves to decides: a
    synthetic definition means the values came from nowhere but the hook, and a real fixture means
    the hook chose its case — an indirect parametrization with no mark to read.
    """
    name = axis.argnames[0]
    for mark in item.markers_with_origin:
        if mark.name != "parametrize" or not mark.args:
            continue
        if name not in _named(mark.args[0]):
            continue
        return Kind.INDIRECT if _is_indirect(mark.kwargs.get("indirect")) else Kind.MARK
    fixture = item.resolve(name)
    if fixture is None or fixture.direct_param:
        return Kind.GENERATED
    if _carries(fixture, axis):
        return Kind.FIXTURE
    return Kind.INDIRECT


def _carries(fixture: FixtureDef, axis: Axis) -> bool:
    """Whether these are the fixture's own `params=`, rather than values handed to it.

    Compared value by value, because a fixture with cases of its own can still be handed different
    ones by a call site, and reading the axis as the fixture's would leave those values nowhere.
    """
    if fixture.params is None or len(fixture.params) != len(axis.values):
        return False
    return all(row[0] == value for row, value in zip(axis.values, fixture.params, strict=True))


def _is_indirect(spelled: str | None) -> bool:
    """Whether a mark's `indirect=` reaches the name at all, however it was written.

    `indirect=True` is the whole mark's; a sequence names the arguments it applies to, and a mark
    that indirects some of its names and not others is read as indirect so that it is refused
    rather than half-translated.
    """
    if spelled is None:
        return False
    try:
        value = ast.literal_eval(spelled)
    except (ValueError, SyntaxError):
        return True
    return bool(value)


def _named(argnames: str) -> tuple[str, ...]:
    """The names one `parametrize` mark covers, read from the `repr` the dump carries."""
    try:
        value = ast.literal_eval(argnames)
    except (ValueError, SyntaxError):
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(part) for part in value)
    return ()


def _qualname_of(item: Item) -> str:
    return f"{item.cls}.{item.originalname}" if item.cls else item.originalname


# --- the shape of one test's case list ----------------------------------------------------------


def _grouped(specs: Sequence[CallSpec]) -> Iterator[tuple[str, ...]]:
    names = list(specs[0].indices)
    grouped: dict[tuple[int, ...], list[str]] = {}
    for name in names:
        signature = tuple(spec.indices.get(name, -1) for spec in specs)
        grouped.setdefault(signature, []).append(name)
    for group in grouped.values():
        yield tuple(group)


def _values(specs: Sequence[CallSpec], argnames: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """The `repr` of each argname's value, one row per case index, in index order."""
    by_index: dict[int, tuple[str, ...]] = {}
    for spec in specs:
        index = spec.indices.get(argnames[0])
        if index is None or index in by_index:
            continue
        by_index[index] = tuple(spec.params.get(name, "") for name in argnames)
    return tuple(by_index[index] for index in sorted(by_index))


def _position_of(
    specs: Sequence[CallSpec], argnames: tuple[str, ...]
) -> tuple[int | None, tuple[str, ...] | None]:
    by_index: dict[int, list[CallSpec]] = {}
    for spec in specs:
        index = spec.indices.get(argnames[0])
        if index is None:
            return None, None
        by_index.setdefault(index, []).append(spec)
    if len(by_index) < 2 and len(specs) > 1:
        # An axis with one value explains nothing about which part of the id is its own.
        return None, None
    width = len(specs[0].idlist)
    if any(len(spec.idlist) != width for spec in specs):
        return None, None
    candidates = [
        position
        for position in range(width)
        if _constant_within(by_index, position) and _distinct_across(by_index, position)
    ]
    if len(candidates) != 1:
        return None, None
    position = candidates[0]
    return position, tuple(by_index[index][0].idlist[position] for index in sorted(by_index))


def _constant_within(by_index: Mapping[int, list[CallSpec]], position: int) -> bool:
    return all(len({spec.idlist[position] for spec in group}) == 1 for group in by_index.values())


def _distinct_across(by_index: Mapping[int, list[CallSpec]], position: int) -> bool:
    seen = [group[0].idlist[position] for group in by_index.values()]
    return len(set(seen)) == len(seen)
