"""Fixture objects, the `Depends` sentinel, and the static injection/resolution plans.

This module owns the objects the rest of velox is built on: `Fixture`, what `@velox.fixture()`
returns, the sentinel `Depends()` leaves on a parameter, and `ResolutionPlan`, the flattened,
statically-validated, topologically-sorted construction order for one test function's whole
transitive fixture graph.

Both are built once, at decoration/collection time, by walking `__code__.co_varnames` against
`__defaults__`/`__kwdefaults__` and the annotations — never `inspect.signature`, never
`get_type_hints`. Annotations are load-bearing for exactly one thing: a `Depends(...)` marker in
`Annotated[...]` metadata, which `_annotated_injections` finds by *parsing* the annotation and
evaluating only that marker. The type half is never evaluated, so it stays what it always was —
for the reader and the type checker.

`ResolutionPlan` is a flat list, not a tree: `_di.py` executes it as a straight loop with no graph
walking at run time. Building it *is* the graph walk, done once per test function and memoized so
a diamond-shaped graph produces one step per fixture rather than one per path to it — except
`scope="call"` fixtures, which are never memoized, since each `Depends()` site pointing at one
needs its own step and its own cache key. Construction, caching, and teardown are `_di.py`'s job;
this module only decides *what* needs building and *in what order* — the declarative half of the
DI system, with `_di.py` as the dynamic half.

A fixture built with `params=` multiplies every test transitively depending on it: `plan_for`
walks the graph once, tagging each step with the parametrized fixtures in its own ancestry, and
`expand_cases` turns that one plan into one specialized `ResolutionPlan` per combination of their
cases, mirroring `@velox.parametrize`'s own case expansion for the fixture graph instead of a
test's own arguments.
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import inspect
import itertools
import re
import sys
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import (
    Annotated,
    Any,
    Literal,
    Protocol,
    TypeAliasType,
    cast,
    final,
    get_args,
    get_origin,
    overload,
)

if sys.version_info >= (3, 14):
    import annotationlib

__all__ = [
    "BuiltinContext",
    "BuiltinProvider",
    "DIError",
    "Depends",
    "ExpandedPlan",
    "Fixture",
    "Injection",
    "PlanStep",
    "ResolutionPlan",
    "Scope",
    "builtin_fixture",
    "case_value_id",
    "dedupe_case_ids",
    "exclusive_tokens_of",
    "expand_cases",
    "fixture",
    "plan_for",
    "plan_of",
]

type Closer = Callable[[], Awaitable[None]]
"""Mirrors `_di.Closer`. Nothing enforces that the two definitions match beyond this comment."""

type Scope = Literal["call", "function", "module", "session"]
"""How widely one constructed instance is shared.

- `"call"` — never shared. A fresh instance per `Depends(...)` site, so a test asking for the
  same fixture twice gets two distinct values. Teardown still runs at end of test.
- `"function"` — one instance per test (the default).
- `"module"` — one instance per test module.
- `"session"` — one instance per run.
"""

type Exclusive = bool | str
"""`True` for a private token, or a shared string token naming the contended resource."""

_UNRESOLVED = object()
"""`_lookup`'s "no such name", distinct from a name that legitimately resolves to `None`."""

_MAX_ALIAS_HOPS = 8
"""How far `_markers_in_object` follows `type X = Y` before giving up.

Bounded rather than a `while`, because a PEP 695 alias body is evaluated lazily and a
self-referential one (`type A = A`) hands back the alias object itself, forever.
"""

_DOTTED_NAME = re.compile(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*", re.UNICODE)
"""A whole annotation that is just a name, dotted or not — the shape an alias arrives in."""


@final
@dataclass(frozen=True, slots=True)
class Injection:
    """One `Depends(...)` site on a fixture or test callable."""

    param: str
    source: Fixture[Any]
    keyword_only: bool


@final
@dataclass(frozen=True, slots=True)
class Dependency[T]:
    """The run-time sentinel `Depends()` returns. Never seen by user code as itself."""

    fixture: Fixture[T]


def Depends[T](dependency: Fixture[T], /) -> T:
    """Declare an injected parameter: `param: Annotated[Type, Depends(fx)]`, or `param: Type =
    Depends(fx)`.

    Takes the `Fixture` object itself, not a name or a bare callable. Typed as returning `T` so
    the parameter's own annotation is checked normally in default position, though at run time it
    returns a sentinel; in metadata the declared return type is irrelevant. Raises `TypeError`
    immediately if `dependency` isn't a `Fixture`.
    """
    if not isinstance(dependency, Fixture):
        raise TypeError(
            f"Depends() requires a Fixture object, built with @velox.fixture() -- got "
            f"{dependency!r}"
        )
    return cast(T, Dependency(dependency))


def plan_of(func: Callable[..., Any]) -> tuple[Injection, ...]:
    """Read the injection plan off a callable's parameters.

    A parameter is injected iff it declares a `Depends(...)`: as its default
    (`db: Session = Depends(db_fx)`), or as a marker in its annotation's metadata
    (`db: Annotated[Session, Depends(db_fx)]`). Any other default is an ordinary Python default
    and is left alone — that is what keeps `@parametrize` and plain closures working.

    One walk over the signature in declaration order, so the two spellings interleave freely. The
    order it produces is the order the earlier `__defaults__`-then-`__kwdefaults__` pair of loops
    produced, so no `PlanStep` ordering or cache key moves for a suite that adopts neither.
    """
    code = getattr(func, "__code__", None)
    if code is None:
        return ()

    params = code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
    name = getattr(func, "__name__", repr(func))
    annotated = _annotated_injections(func, params, name)
    defaults = func.__defaults__ or ()
    kwdefaults = func.__kwdefaults__ or {}
    first_defaulted = code.co_argcount - len(defaults)

    injections: list[Injection] = []
    for index, param in enumerate(params):
        keyword_only = index >= code.co_argcount
        if keyword_only:
            default = kwdefaults.get(param)
        elif index >= first_defaulted:
            default = defaults[index - first_defaulted]
        else:
            default = None

        from_default = default.fixture if isinstance(default, Dependency) else None
        from_annotation = annotated.get(param)
        if from_default is not None and from_annotation is not None:
            raise DIError(
                f"{name}({param!r}): Depends(...) declared twice, once as the default and once "
                f"in Annotated[...] metadata. Pick one spelling -- velox merges nothing, even "
                f"when both name the same fixture."
            )
        source = from_default if from_default is not None else from_annotation
        if source is None:
            continue

        # `_di` binds every injection by keyword (`**kwargs`, never positionally), so a
        # `Depends(...)` on a positional-only parameter (before `/`) could never actually be
        # supplied at construction time. Rejected here, at decoration time, rather than left to
        # fail later as an opaque `TypeError` naming no fixture. True of either spelling.
        if index < code.co_posonlyargcount:
            raise DIError(
                f"{name}({param!r}): Depends(...) on a positional-only parameter (before "
                f"'/') is not supported -- velox binds every injection by keyword, and a "
                f"positional-only parameter can never accept one. Move {param!r} after the "
                f"'/', or stop injecting it."
            )
        injections.append(Injection(param=param, source=source, keyword_only=keyword_only))

    return tuple(injections)


def _annotated_injections(
    func: Callable[..., Any], params: tuple[str, ...], name: str
) -> Mapping[str, Fixture[Any]]:
    """The `Depends(...)` markers in `func`'s `Annotated[...]` metadata, as `{param: fixture}`.

    Annotations are not reliably objects. Under `from __future__ import annotations` (PEP 563)
    every one is a string, so the `Depends(...)` in it was never called and there is nothing to
    find; on 3.14 (PEP 649) they are computed lazily and *reading* them raises for any name that
    exists only under `TYPE_CHECKING`. The policy both force is **parse, don't evaluate**: velox
    reads the annotation's source text with `ast` and evaluates only the metadata elements that
    are calls to velox's own `Depends`. The type half is never touched, so wherever the module
    itself does not evaluate its annotations, a `TYPE_CHECKING`-only type can be injected against
    and a function whose *other* parameters carry unresolvable annotations still collects.

    Its sharp edge, documented in `docs/reference/fixtures.md`: an annotation is evaluated in
    module globals and cannot see local names, so the fixture named in `Depends(...)` has to be
    reachable there. A fixture held in a closure variable works only in a module that does not
    stringify its annotations.

    Only names that are actual parameters count, so a `functools.wraps` wrapper — which copies
    `__annotations__` but presents `(*args, **kwargs)` — reads as no injections, keeping the
    existing sharp edge (a signature-replacing decorator hides injections) rather than trading it
    for a new false positive.
    """
    annotations = _annotations_of(func)
    if not annotations:
        return {}

    globalns = getattr(func, "__globals__", None)
    if globalns is None:
        return {}
    absorbs = bool(func.__code__.co_flags & (inspect.CO_VARARGS | inspect.CO_VARKEYWORDS))

    found: dict[str, Fixture[Any]] = {}
    for param, annotation in annotations.items():
        if param == "return" or (param not in params and absorbs):
            continue
        markers = (
            _markers_in_source(annotation, globalns, name=name, param=param)
            if isinstance(annotation, str)
            else _markers_in_object(annotation)
        )
        if not markers:
            continue
        if len(markers) > 1:
            raise DIError(
                f"{name}({param!r}): {len(markers)} Depends(...) markers in one Annotated[...] "
                f"-- a parameter is injected from exactly one fixture."
            )
        if param not in params:
            raise DIError(
                f"{name}({param!r}): Depends(...) in the annotation of {param!r}, which is not a "
                f"parameter of this function -- nothing would ever be injected for it."
            )
        found[param] = markers[0].fixture
    return found


def _annotations_of(func: Callable[..., Any]) -> Mapping[str, Any]:
    """`func`'s annotations, evaluating none of them.

    On 3.14+ `func.__annotations__` *computes* them and raises for a name that exists only under
    `TYPE_CHECKING`, so ask `annotationlib` for source text instead: every entry arrives as a
    string, whether or not the module stringifies its annotations, and nothing is executed. A
    function with no annotations at all has `__annotate__ is None`, the cheap bail-out that keeps
    this free for a suite that annotates nothing.

    On 3.13 `__annotations__` is whatever the module already produced — objects, or strings under
    PEP 563 — and reading it never raises.
    """
    if sys.version_info >= (3, 14):
        if getattr(func, "__annotate__", None) is None:
            return {}
        try:
            return annotationlib.get_annotations(func, format=annotationlib.Format.STRING)
        except Exception:  # a hand-written `__annotate__`; not velox's to repair
            return {}
    return getattr(func, "__annotations__", None) or {}


def _markers_in_object(annotation: object, depth: int = 0) -> list[Dependency[Any]]:
    """The `Dependency` markers in an annotation that is already an object.

    An alias is followed to its body, a *subscripted* one included: `typing` leaves `Repo[int]`,
    for a `type Repo[T] = Annotated[T, Depends(repo_fx)]`, as the alias applied to its argument,
    so the metadata is reachable only by unwrapping it deliberately — `get_type_hints` does not do
    it either. Substituting the parameter cannot change what the metadata holds, so the markers
    are read off the alias body unsubstituted.

    The type half is followed too, since `Annotated[Db, "documentation"]` holds its marker there
    whenever `Db` is an alias `typing` had nothing to flatten at construction. Both walks are
    bounded rather than a `while`: `type A = A` hands back the alias object itself, forever.
    """
    for _ in range(_MAX_ALIAS_HOPS):
        try:
            alias = annotation if isinstance(annotation, TypeAliasType) else get_origin(annotation)
            if not isinstance(alias, TypeAliasType):
                break
            annotation = alias.__value__
        except Exception:  # a PEP 695 alias is lazy, and its body may not resolve at run time
            return []
    try:
        if get_origin(annotation) is not Annotated:
            return []
        head, *metadata = get_args(annotation)
    except Exception:  # never let an exotic annotation object break collection
        return []
    markers = [m for m in metadata if isinstance(m, Dependency)]
    if depth >= _MAX_ALIAS_HOPS:
        return markers
    return _markers_in_object(head, depth + 1) + markers


def _markers_in_source(
    text: str, globalns: dict[str, Any], *, name: str, param: str
) -> list[Dependency[Any]]:
    """The `Dependency` markers in an annotation that arrived as source text.

    A bare dotted name is an alias — `db: Db` for a module-level
    `type Db = Annotated[Session, Depends(db_fx)]` — and is resolved by dictionary lookup, no
    parse. Anything without a subscript has nowhere to hold metadata. What is left is parsed, and
    only its metadata elements are looked at: a `Name` resolved the same way, a `Call` compiled
    and evaluated iff its callee is velox's own `Depends`. The gate is the shape of the
    annotation, not the spelling of the names in it, since both `Annotated` and `Depends` can
    arrive under any name a user imported them as.
    """
    if _DOTTED_NAME.fullmatch(text):
        return _markers_in_object(_lookup_parts(text.split("."), globalns))
    if "[" not in text:
        return []
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError:  # an annotation Python itself would reject; leave it to Python
        return []
    return _markers_in_node(node, globalns, name=name, param=param)


def _markers_in_node(
    node: ast.expr, globalns: dict[str, Any], *, name: str, param: str
) -> list[Dependency[Any]]:
    """The markers in one parsed annotation node: an `Annotated[...]`, or an alias of one.

    `Annotated[Annotated[X, a], b]` carries both markers, because that is the single flattened
    object `typing` builds from it — and what the object path therefore sees. A name in any
    position an annotation can hold one is resolved by lookup and handed to the object path, so
    an alias reads the same whether or not the module stringifies its annotations: as the whole
    annotation (`db: Db`), as the target of a subscript (`db: Repo[int]`), or as the type half of
    an `Annotated` wrapping it (`db: Annotated[Db, "documentation"]`).
    """
    if isinstance(node, ast.Name | ast.Attribute):
        return _markers_in_object(_lookup(node, globalns))
    if not isinstance(node, ast.Subscript):
        return []
    if not _is_annotated(node.value, globalns):
        # Not an `Annotated[...]`, but `Repo[int]` may still name an alias that is one.
        return _markers_in_object(_lookup(node.value, globalns))
    elements = node.slice.elts if isinstance(node.slice, ast.Tuple) else ()
    if not elements:
        return []
    head, *metadata = elements
    markers = [_marker_of(element, globalns, name=name, param=param) for element in metadata]
    return _markers_in_node(head, globalns, name=name, param=param) + [
        marker for marker in markers if marker is not None
    ]


def _marker_of(
    element: ast.expr, globalns: dict[str, Any], *, name: str, param: str
) -> Dependency[Any] | None:
    """One `Annotated[...]` metadata element, as a `Dependency` if that is what it denotes."""
    if isinstance(element, ast.Name | ast.Attribute):
        found = _lookup(element, globalns)
        return found if isinstance(found, Dependency) else None
    # Anything that is not a call to velox's own `Depends` is left entirely alone: metadata holds
    # arbitrary objects, and velox must not execute arbitrary annotation code to look at them.
    if not isinstance(element, ast.Call) or _lookup(element.func, globalns) is not Depends:
        return None
    try:
        # One `Depends(...)` call node, off this module's own parse of the annotation.
        value = eval(compile(ast.Expression(body=element), "<velox annotation>", "eval"), globalns)
    except Exception as exc:
        hint = (
            " An annotation is evaluated in the module's globals and cannot see local names, so "
            "the fixture it names has to be reachable there."
            if isinstance(exc, NameError)
            else ""
        )
        raise DIError(
            f"{name}({param!r}): the Depends(...) in this annotation raised "
            f"{type(exc).__name__}: {exc}.{hint}"
        ) from exc
    return value if isinstance(value, Dependency) else None


def _is_annotated(node: ast.expr, globalns: dict[str, Any]) -> bool:
    """Whether a subscript's target is `typing.Annotated`, however it is spelled."""
    found = _lookup(node, globalns)
    if found is not _UNRESOLVED:
        return found is Annotated
    # Unresolvable, which a stringifying module may legitimately produce by importing `Annotated`
    # itself only under `TYPE_CHECKING`. Fall back to the spelling: nothing is evaluated on the
    # strength of it, since a marker still has to resolve to velox's own `Depends` by identity.
    return isinstance(node, ast.Name | ast.Attribute) and _tail_name(node) == "Annotated"


def _lookup(node: ast.expr, globalns: dict[str, Any]) -> Any:
    """Resolve a dotted name against `globalns` by lookup and `getattr` — never `eval`.

    `_UNRESOLVED` for anything that is not a dotted name, or whose head is not a global.
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return _UNRESOLVED
    return _lookup_parts([node.id, *reversed(parts)], globalns)


def _lookup_parts(parts: Sequence[str], globalns: dict[str, Any]) -> Any:
    """`parts` walked from `globalns`, or `_UNRESOLVED`.

    A name that resolves to nothing is not an error: most annotations are types velox has no
    interest in, and plenty of them do not resolve at run time at all. `getattr` is guarded for
    the same reason it is called at all — a module's `__getattr__` runs arbitrary code, and a
    lazy importer raising for an unrelated parameter's annotation must not fail the test.
    """
    found = globalns.get(parts[0], _UNRESOLVED)
    for part in parts[1:]:
        if found is _UNRESOLVED:
            return _UNRESOLVED
        try:
            found = getattr(found, part)
        except Exception:
            return _UNRESOLVED
    return found


def _tail_name(node: ast.Name | ast.Attribute) -> str:
    """The last component of a dotted name: `Annotated`, `typing.Annotated`, `t.Annotated`."""
    return node.attr if isinstance(node, ast.Attribute) else node.id


@final
@dataclass(frozen=True, slots=True)
class BuiltinContext:
    """The per-setup-call context a `BuiltinProvider` needs beyond its own injected `kwargs`:
    everything `_di.setup` already has to hand from its own parameters.

    A provider reaches other per-test facts — the capture sink, the log-record buffer, the
    concurrency-slot index, marks, the timeout budget — through `ContextVar`s set around each
    test's setup/call/teardown envelope.
    """

    test_id: str
    module_path: str


type BuiltinProvider = Callable[
    [Mapping[str, Any], BuiltinContext], Awaitable[tuple[Any, Closer | None]]
]
"""What a runtime-supplied (as opposed to user-written) fixture hands `_di._construct` instead of
a call to `Fixture.func` — the same `(value, closer)` shape `_construct` already produces for
every other fixture kind, so nothing downstream of construction (caching, refcounting, teardown)
needs to know the difference. See `builtin_fixture` below."""


#: The name a parametrized fixture's own function must accept its case value through — velox has
#: no `request` object, so `params=` reuses the same name-based convention `@velox.parametrize`
#: already uses for its own call kwargs, fixed to one name since a fixture (unlike a test) only
#: ever has one `params=` axis.
_PARAM_NAME = "param"


def case_value_id(
    value: object, argname: str, index: int, idfn: Callable[[object], str | None] | None = None
) -> str:
    """The display id for one parametrized value: `idfn(value)` when it returns a `str`,
    otherwise the literal for `bool`/`str`/`int`/`None`/`enum.Enum`, otherwise
    `f"{argname}{index}"`. Shared by `@velox.parametrize`'s own id generation
    (`_collection.parametrize`) and by a parametrized fixture's `case_ids`.
    """
    if idfn is not None:
        try:
            generated = idfn(value)
        except Exception:  # a broken id callable must not abort collection -- fall back instead
            generated = None
        if isinstance(generated, str):
            return generated
    # `bool` before `int`: `isinstance(True, int)` is true, and `str(True)` ("True") is the
    # readable id -- landing in a combined `int`/`str`/`bool` branch first would give "1" instead.
    if isinstance(value, bool | str | int) or value is None:
        return str(value)
    if isinstance(value, enum.Enum):
        return str(value.name)
    return f"{argname}{index}"


def dedupe_case_ids(ids: Sequence[str]) -> tuple[str, ...]:
    """Disambiguate a sequence of generated ids that may collide.

    Every id that occurs exactly once is reserved as-is. Each id sharing a duplicate then gets
    the lowest `f"{id}{n}"` not already reserved -- checked against every id reserved so far, not
    just its own collision group, so a renamed id can never land on one either already unique or
    already claimed by an earlier rename. Shared by `@velox.parametrize`'s own case expansion
    (`_collection.parametrize`) and by a parametrized fixture's `case_ids`.
    """
    ids = tuple(ids)
    counts = Counter(ids)
    used = {id_ for id_, count in counts.items() if count == 1}
    next_occurrence: dict[str, int] = {}
    result: list[str] = []
    for id_ in ids:
        if counts[id_] == 1:
            result.append(id_)
            continue
        occurrence = next_occurrence.get(id_, 0)
        candidate = f"{id_}{occurrence}"
        while candidate in used:
            occurrence += 1
            candidate = f"{id_}{occurrence}"
        next_occurrence[id_] = occurrence + 1
        used.add(candidate)
        result.append(candidate)
    return tuple(result)


def _normalize_params(
    fixture_name: str,
    params: Sequence[object] | None,
    ids: Sequence[str] | Callable[[object], str | None] | None,
) -> tuple[tuple[object, ...], tuple[str, ...]]:
    """`(params, ids)` as given to `fixture()`, validated and turned into `Fixture._params`/
    `Fixture._case_ids`. `params=None` (the default, "not parametrized") returns `((), ())`.
    """
    if params is None:
        if ids is not None:
            raise ValueError(
                f"fixture {fixture_name!r}: ids= given without params= -- there is nothing for "
                f"it to label"
            )
        return (), ()
    values = tuple(params)
    if not values:
        # Caught here rather than left to expand into zero cases: a `params=()` fixture would
        # otherwise vanish every test depending on it from the suite with no CollectionError and
        # no count discrepancy visible at a glance -- the same failure mode `@velox.parametrize`
        # guards against for an empty `argvalues`.
        raise ValueError(f"fixture {fixture_name!r}: params=() -- no values given")
    fixed_ids = ids if ids is None or callable(ids) else tuple(ids)
    if isinstance(fixed_ids, tuple) and len(fixed_ids) != len(values):
        raise ValueError(
            f"fixture {fixture_name!r}: {len(fixed_ids)} id(s) for {len(values)} params value(s)"
        )
    raw_ids = (
        fixed_ids
        if isinstance(fixed_ids, tuple)
        else tuple(
            case_value_id(value, _PARAM_NAME, index, fixed_ids)
            for index, value in enumerate(values)
        )
    )
    return values, dedupe_case_ids(raw_ids)


@final
class Fixture[T]:
    """A fixture: the callable, plus everything the scheduler needs to know statically.

    Constructed by `@velox.fixture()`. Immutable.
    """

    __slots__ = (
        "_case_ids",
        "_exclusive",
        "_func",
        "_name",
        "_params",
        "_plan",
        "_provider",
        "_scope",
    )

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        scope: Scope = "function",
        exclusive: Exclusive = False,
        name: str | None = None,
        provider: BuiltinProvider | None = None,
        params: Sequence[object] | None = None,
        ids: Sequence[str] | Callable[[object], str | None] | None = None,
    ) -> None:
        self._func = func
        self._scope: Scope = scope
        self._exclusive: Exclusive = exclusive
        self._name = name if name is not None else getattr(func, "__name__", repr(func))
        self._params, self._case_ids = _normalize_params(self._name, params, ids)
        self._plan = plan_of(func)
        self._provider = provider
        # Checked here rather than only when some test's `plan_for` walk reaches this fixture: a
        # fixture can only ever be called with the parameters `Depends(...)` supplies (there is no
        # name-based lookup to fall back on), so a required, non-injected parameter is broken by
        # construction regardless of who depends on it. Checking at build time means it fires once
        # per fixture, not once per path a diamond graph reaches it by, and fires even for a
        # fixture no collected test currently uses. `_PARAM_NAME` is threaded through the same
        # `known_params` mechanism `@velox.parametrize` uses on a test, so a parametrized
        # fixture's `param` argument reads as supplied rather than missing.
        known_params: frozenset[str] = frozenset({_PARAM_NAME}) if self._params else frozenset()
        _check_missing_injections(func, self._plan, known_params=known_params)
        _check_acyclic(self)

    @property
    def func(self) -> Callable[..., Any]:
        """The undecorated callable. Sync, async, sync generator, or async generator."""
        return self._func

    @property
    def name(self) -> str:
        """Display name in errors, reports, and `--durations`."""
        return self._name

    @property
    def scope(self) -> Scope:
        """How widely one constructed instance of this fixture is shared."""
        return self._scope

    @property
    def exclusive(self) -> Exclusive:
        """The contention token, or `False` for a fixture that isn't exclusive."""
        return self._exclusive

    @property
    def provider(self) -> BuiltinProvider | None:
        """Set on velox's own built-in fixtures, whose value the runtime supplies directly
        rather than by calling `func`. `None` for every fixture built with `velox.fixture()`."""
        return self._provider

    @property
    def plan(self) -> tuple[Injection, ...]:
        """This fixture's own `Depends(...)` sites."""
        return self._plan

    @property
    def params(self) -> tuple[object, ...]:
        """Case values from `params=`, empty for a fixture that isn't parametrized."""
        return self._params

    @property
    def case_ids(self) -> tuple[str, ...]:
        """Display id for each of `params`, same length and same order -- generated the same way
        `@velox.parametrize`'s are."""
        return self._case_ids

    @property
    def dependencies(self) -> tuple[Fixture[Any], ...]:
        """The fixtures this one depends on, in the order of its own `Depends(...)` sites.

        Acyclic by construction: a cycle is rejected when the fixture is declared.
        """
        return tuple(i.source for i in self._plan)

    def __call__(self, *args: object, **kwargs: object) -> Any:
        """Call the underlying function directly, as ordinary Python.

        No injection is performed: any un-passed `Depends(...)` parameter keeps its sentinel
        default. Overriding one dependency for one test means writing a separate fixture with
        the replacement wired in and passing that to `Depends(...)` at the call site.
        """
        return self._func(*args, **kwargs)

    def __repr__(self) -> str:
        exclusive = "" if self._exclusive is False else f" exclusive={self._exclusive!r}"
        return f"<Fixture {self._name} scope={self._scope}{exclusive}>"


def _check_acyclic(root: Fixture[Any]) -> None:
    """Raise if `root`'s dependency graph loops back on itself.

    Keyed by `id()`: `Fixture` has no structural equality, so identity is the only available
    notion of "same fixture".
    """
    on_path: set[int] = set()

    def walk(node: Fixture[Any]) -> None:
        node_id = id(node)
        if node_id in on_path:
            raise ValueError(f"dependency cycle detected at fixture {node.name!r}")
        on_path.add(node_id)
        for dep in node.dependencies:
            walk(dep)
        on_path.discard(node_id)

    walk(root)


class FixtureDecorator(Protocol):
    """What `velox.fixture(...)` returns: a decorator that unwraps the yielded/awaited type."""

    @overload
    def __call__[T](self, fn: Callable[..., AsyncIterator[T]], /) -> Fixture[T]: ...
    @overload
    def __call__[T](self, fn: Callable[..., Iterator[T]], /) -> Fixture[T]: ...
    @overload
    def __call__[T](self, fn: Callable[..., Awaitable[T]], /) -> Fixture[T]: ...
    @overload
    def __call__[T](self, fn: Callable[..., T], /) -> Fixture[T]: ...


def fixture(
    *,
    scope: Scope = "function",
    exclusive: Exclusive = False,
    name: str | None = None,
    params: Sequence[object] | None = None,
    ids: Sequence[str] | Callable[[object], str | None] | None = None,
) -> FixtureDecorator:
    """Declare a fixture. Parentheses are always required, even with no arguments.

    :param scope: how widely one constructed instance is shared. See `Scope`.
    :param exclusive: `True`, or a string token naming a contended resource. Tests transitively
        depending on it never run concurrently with each other.
    :param name: display name in errors, reports, and `--durations`. Defaults to `fn.__name__`.
    :param params: case values that multiply every test transitively depending on this fixture,
        one collected test per value. The decorated function receives each case through a
        parameter literally named `param` -- velox has no `request` object, so this is the same
        name-based convention `@velox.parametrize` uses for its own call kwargs.
    :param ids: display id per `params` entry: a same-length sequence of strings, or a callable
        taking one value and returning a `str` (or `None`, to fall back to the automatic id for
        that value). Only valid alongside `params`.
    """

    def decorate(fn: Callable[..., Any], /) -> Fixture[Any]:
        return Fixture(fn, scope=scope, exclusive=exclusive, name=name, params=params, ids=ids)

    return cast(FixtureDecorator, decorate)


def builtin_fixture[T](declared: Fixture[T], /, *, provider: BuiltinProvider) -> Fixture[T]:
    """Rebuild `declared` as a fixture whose value the velox runtime supplies directly, instead of
    by calling its function.

    Scope, exclusivity, name and value type all come from `declared`, which stays the one place
    each built-in is declared; `_di._construct` checks `.provider` first, so the function's body
    never runs. `params`/`ids` are not carried, because the value comes from the provider and
    there is nothing for a case to vary. Not exposed through `fixture()`; only
    `_builtins/fixtures.py` calls this.
    """
    return Fixture(
        declared.func,
        scope=declared.scope,
        exclusive=declared.exclusive,
        name=declared.name,
        provider=provider,
    )


class DIError(Exception):
    """A static DI validation failure: scope nesting, a missing injection.

    Raised by `plan_for`, always before any test runs. `_collect.collect` catches this and turns
    it into a `CollectionError` attributed to the offending test, alongside every other kind of
    collection failure.
    """


#: SESSION > MODULE > FUNCTION; a fixture may depend only on equal-or-wider scopes. `"call"` is
#: ranked with `"function"`: both tear down at end of test, and the compatibility rule only cares
#: about *lifetime*, not the caching behavior that otherwise tells the two apart.
_SCOPE_RANK: dict[Scope, int] = {"session": 3, "module": 2, "function": 1, "call": 1}


#: `PlanStep.param_value`'s value when this step's own fixture isn't parametrized, or hasn't yet
#: been specialized by `expand_cases` -- never a real case value, so `is _NO_PARAM` is unambiguous.
_NO_PARAM = object()


@final
@dataclass(frozen=True, slots=True)
class PlanStep:
    """One fixture construction, in an already-topologically-sorted `ResolutionPlan.steps`.

    `args` names this fixture's own `Depends(...)` parameters, each resolved to the `step_id` of
    an earlier step in the same plan — executing a plan is array indexing, not graph walking.
    """

    step_id: int
    fixture: Fixture[Any]
    args: tuple[tuple[str, int, bool], ...]
    """`(param, source step_id, keyword_only)` per `Depends(...)` site on this fixture."""
    param_ancestors: tuple[int, ...] = ()
    """`id(fixture)` of every parametrized fixture in this step's own transitive dependency
    closure, including itself when its own fixture is parametrized — in canonical
    (first-discovered) order. Empty when this step's construction never varies with any
    parametrized fixture's case. Set by `plan_for`; consumed only by `expand_cases`, which turns
    it into `case_key`/`param_value` below and is meaningless afterward."""
    case_key: tuple[tuple[int, int], ...] = ()
    """`(id(fixture), chosen case index)` for every fixture named in `param_ancestors`, in the
    same order — folded into this step's `runtime.key_for` cache key so two specialized plans
    that chose different cases never share a construction. Set by `expand_cases`; empty on a step
    it never touched (nothing upstream is parametrized) or on `plan_for`'s own un-expanded plan."""
    param_value: object = _NO_PARAM
    """The chosen case value, passed to this step's fixture body as its `param` argument, iff
    `fixture.params` is non-empty. Set by `expand_cases`; `_NO_PARAM` otherwise."""


@final
@dataclass(frozen=True, slots=True)
class ResolutionPlan:
    """The whole transitive fixture graph one test function needs, flattened and ordered.

    `steps` is dependency-before-dependent, a valid topological order of the graph; `_di.py`
    constructs by walking it forwards and tears down by walking it backwards, producing
    dependent-before-dependency teardown order.

    A fixture reachable by more than one path (a diamond) gets exactly one step, except
    `scope="call"` fixtures, each of which gets its own.
    """

    steps: tuple[PlanStep, ...]
    root_args: tuple[tuple[str, int, bool], ...]
    """The test function's own `Depends(...)` sites, resolved the same way as `PlanStep.args`. A
    step nothing here points at came from a `velox.use(...)` declaration: built, cached and torn
    down with the rest, its value never reaching the test."""
    param_ancestors: tuple[int, ...] = ()
    """Union, in canonical order, of the `param_ancestors` of every step this test reaches
    directly — through a `Depends(...)` site or a `velox.use(...)` declaration — i.e. the
    parametrized fixtures it transitively depends on. Empty for a plan `expand_cases` passes
    through unchanged."""


def plan_for(
    func: Callable[..., Any],
    *,
    known_params: frozenset[str] = frozenset(),
    implicit: Sequence[Fixture[Any]] = (),
    positional_supplied: int = 0,
) -> ResolutionPlan:
    """Build `func`'s `ResolutionPlan`: a topological walk over its `Depends(...)` graph.

    `func` is a test function; an already-decorated `Fixture` never needs this, since its
    dependencies are already `Fixture.dependencies`/`Fixture.plan`. Raises `DIError` for scope
    compatibility and missing injections — the two checks the graph shape alone can decide.
    Cycles and `Depends()` on a non-`Fixture` are caught earlier, at `Fixture.__init__` and at
    `Depends()`'s own call site.

    `known_params` names parameters `@velox.parametrize`'s argnames supply at call time, so the
    missing-injection check doesn't mistake them for an unsatisfiable `Depends()` site. They never
    become part of the plan itself: expansion hands each case's values straight to `func`,
    alongside this plan's own kwargs.

    `implicit` names fixtures `func`'s containers declared with `velox.use(...)`, in the order they
    apply. Each gets a step and is constructed like any other, but binds to no parameter: it is
    absent from `root_args`, so `func` never sees its value. They are walked before `func`'s own
    `Depends(...)` sites, which is what puts them earliest in `steps` and therefore first to
    construct and last to tear down.

    `positional_supplied` counts leading positional parameters a decorator wrapping `func` fills
    in itself at call time -- `@mock.patch`'s mock objects, the one case velox recognizes
    (`_mocking`). Like `known_params`, they are supplied rather than injected, so they never
    become part of the plan.
    """
    root_injections = plan_of(func)
    # Only the test function's own missing-injection check happens here; every fixture `visit`
    # below reaches already had its own body checked at `Fixture.__init__` time, independent of
    # who depends on it.
    _check_missing_injections(
        func, root_injections, known_params=known_params, positional_supplied=positional_supplied
    )

    steps: list[PlanStep] = []
    #: `id(fixture) -> step_id`, non-`"call"` scopes only — the memo that turns a diamond into
    #: one step. Keyed by `id()`, not the `Fixture` itself: `Fixture` has no structural equality.
    memo: dict[int, int] = {}

    def visit(source: Fixture[Any], dependent: Fixture[Any] | None) -> int:
        """Ensure `source` has a step, return its `step_id`. `dependent` is `None` for the test
        function's own direct dependencies — used only for the scope check and error text."""
        dependent_scope: Scope = "function" if dependent is None else dependent.scope
        if _SCOPE_RANK[source.scope] < _SCOPE_RANK[dependent_scope]:
            holder = "the test itself" if dependent is None else f"fixture {dependent.name!r}"
            raise DIError(
                f"{holder} (scope={dependent_scope!r}) depends on fixture {source.name!r} "
                f"(scope={source.scope!r}): a narrower-scoped fixture would be torn down while "
                f"the wider one still held it."
            )
        if source.scope != "call":
            cached = memo.get(id(source))
            if cached is not None:
                return cached
        args = tuple(
            (injection.param, visit(injection.source, source), injection.keyword_only)
            for injection in source.plan
        )
        ancestors = _ordered_union(
            *(steps[dep_step_id].param_ancestors for _, dep_step_id, _ in args),
            (id(source),) if source.params else (),
        )
        step_id = len(steps)
        steps.append(
            PlanStep(step_id=step_id, fixture=source, args=args, param_ancestors=ancestors)
        )
        if source.scope != "call":
            memo[id(source)] = step_id
        return step_id

    implicit_step_ids = [visit(source, None) for source in implicit]
    root_args = tuple(
        (injection.param, visit(injection.source, None), injection.keyword_only)
        for injection in root_injections
    )
    # Implicit steps count towards `param_ancestors` exactly like a directly-`Depends()`ed one: a
    # `params=` fixture reached only through `velox.use(...)` still has to fan the test out into
    # one case each, and its step still needs `expand_cases` to hand it a `param_value`.
    param_ancestors = _ordered_union(
        *(steps[step_id].param_ancestors for step_id in implicit_step_ids),
        *(steps[step_id].param_ancestors for _, step_id, _ in root_args),
    )
    return ResolutionPlan(steps=tuple(steps), root_args=root_args, param_ancestors=param_ancestors)


def _ordered_union(*sequences: tuple[int, ...]) -> tuple[int, ...]:
    """Every item across `sequences`, deduplicated, in first-seen order — an ordered-set union.
    `dict.setdefault` rather than a plain `set`: iteration order over a `set` of `id()` values is
    hash-bucket order, not insertion order, and `plan_for`'s canonical param-ancestor order needs
    to be deterministic given the same graph."""
    seen: dict[int, None] = {}
    for sequence in sequences:
        for item in sequence:
            seen.setdefault(item, None)
    return tuple(seen)


@final
@dataclass(frozen=True, slots=True)
class ExpandedPlan:
    """One `ResolutionPlan.param_ancestors`-driven specialization of a `plan_for` plan: every
    parametrized fixture it transitively depends on baked to one chosen case each.
    """

    plan: ResolutionPlan
    case_id: str | None
    """Each parametrized fixture's chosen `Fixture.case_ids` entry, joined with `-` in
    `param_ancestors` order. `None` for the one `ExpandedPlan` a plan with no parametrized
    ancestor produces."""


def expand_cases(plan: ResolutionPlan) -> tuple[ExpandedPlan, ...]:
    """Fan `plan` out into one specialized `ResolutionPlan` per combination of cases across every
    parametrized fixture it transitively depends on (`plan.param_ancestors`) — the cartesian
    product, same idea as `_collection.parametrize.cases_for` but for fixture-level `params=`
    instead of `@velox.parametrize`.

    A plan with no parametrized ancestor returns exactly `(ExpandedPlan(plan, None),)` — `plan`
    itself, unchanged and uncopied, so a caller comparing plan identity across un-parametrized
    cases (e.g. two `@velox.parametrize` cases of the same function) still sees the same object.
    """
    if not plan.param_ancestors:
        return (ExpandedPlan(plan=plan, case_id=None),)

    by_id: dict[int, Fixture[Any]] = {id(step.fixture): step.fixture for step in plan.steps}
    axes = [range(len(by_id[fixture_id].params)) for fixture_id in plan.param_ancestors]

    expansions: list[ExpandedPlan] = []
    for combo in itertools.product(*axes):
        chosen: dict[int, int] = dict(zip(plan.param_ancestors, combo, strict=True))
        new_steps = tuple(
            # A step with no parametrized ancestor at all gets back the exact `case_key=()`/
            # `param_value=_NO_PARAM` it already has -- reused as-is rather than replaced, so a
            # plan with one parametrized leaf among many unrelated fixtures doesn't reallocate
            # every unrelated step once per case combination.
            step
            if not step.param_ancestors
            else dataclasses.replace(
                step,
                case_key=tuple((fid, chosen[fid]) for fid in step.param_ancestors),
                param_value=(
                    step.fixture.params[chosen[id(step.fixture)]]
                    if step.fixture.params
                    else _NO_PARAM
                ),
            )
            for step in plan.steps
        )
        case_id = "-".join(
            by_id[fixture_id].case_ids[chosen[fixture_id]] for fixture_id in plan.param_ancestors
        )
        expansions.append(
            ExpandedPlan(
                plan=ResolutionPlan(
                    steps=new_steps, root_args=plan.root_args, param_ancestors=plan.param_ancestors
                ),
                case_id=case_id,
            )
        )
    return tuple(expansions)


def exclusive_tokens_of(plan: ResolutionPlan) -> frozenset[object]:
    """The exclusive-resource tokens `plan`'s fixtures collectively hold.

    One token per `exclusive=`-marked fixture reachable in `plan.steps`: the `Fixture` object
    itself for `exclusive=True` (private to that one fixture), or the string for
    `exclusive="name"` (shared with every other fixture naming the same string). Two tests whose
    token sets intersect declare the same contended resource and must never run concurrently.
    """
    return frozenset(
        step.fixture if step.fixture.exclusive is True else step.fixture.exclusive
        for step in plan.steps
        if step.fixture.exclusive is not False
    )


def _reject_params(name: str, offenders: frozenset[str], message: str) -> None:
    """Raise `DIError` naming `offenders`, sorted, iff there are any. Shared by every sanity
    check in `_check_missing_injections` below -- each just supplies the offending set and its
    own explanation of what's wrong with it."""
    if offenders:
        raise DIError(f"{name}: parameter(s) {', '.join(sorted(offenders))} {message}")


def _check_missing_injections(
    func: Callable[..., Any],
    injections: tuple[Injection, ...],
    *,
    known_params: frozenset[str] = frozenset(),
    positional_supplied: int = 0,
) -> None:
    """Raise `DIError` naming any parameter with no default that isn't `self`, isn't injected,
    isn't in `known_params`, and isn't one of the first `positional_supplied` parameters.

    Reads `__code__`/`__defaults__`, the same way `plan_of` does, keeping the two code paths
    consistent. Also rejects a `known_params` name that collides with an actual `Depends(...)`
    injection, that falls before the signature's `/` (both bind by keyword at call time --
    `_di.setup`'s and expansion's kwargs alike -- so a positional-only one could never actually
    receive its value), or that matches none of `func`'s parameters at all (unless `func` takes
    `**kwargs`, which would absorb it same as a real call would); and a `Depends(...)` or
    `known_params` name landing in a parameter a wrapping decorator already fills positionally.
    """
    code = getattr(func, "__code__", None)
    if code is None:
        return
    injected = {injection.param for injection in injections}
    name = getattr(func, "__name__", repr(func))

    _reject_params(
        name,
        known_params & injected,
        "are both Depends(...)-injected and supplied externally (e.g. by @velox.parametrize) "
        "-- pick one source per parameter.",
    )

    positional = code.co_varnames[: code.co_argcount]
    kwonly = code.co_varnames[code.co_argcount : code.co_argcount + code.co_kwonlyargcount]
    # The parameters a decorator wrapping `func` binds positionally before velox's own keyword
    # arguments reach it -- `@mock.patch`'s mock objects arrive in exactly this many leading
    # slots, so nothing else can claim them.
    supplied = frozenset(positional[:positional_supplied])

    _reject_params(
        name,
        supplied & injected,
        "are Depends(...)-injected in a slot the decorator wrapping this test fills itself "
        "(e.g. @mock.patch's mock objects, which arrive first and positionally) -- declare the "
        "injected parameters after them.",
    )
    _reject_params(
        name,
        known_params & supplied,
        "are supplied externally (e.g. by @velox.parametrize) in a slot the decorator wrapping "
        "this test fills itself (e.g. @mock.patch's mock objects, which arrive first and "
        "positionally) -- declare them after those.",
    )

    _reject_params(
        name,
        known_params & set(positional[: code.co_posonlyargcount]),
        "are supplied externally (e.g. by @velox.parametrize) on a positional-only parameter "
        "(before '/') -- velox always calls a test function by keyword. Move it after the '/'.",
    )

    # `code.co_flags`, not `inspect.signature` -- consistent with the rest of this module reading
    # `__code__` directly. `inspect.CO_VARKEYWORDS` is just the flag-bit constant.
    if not (code.co_flags & inspect.CO_VARKEYWORDS):
        _reject_params(
            name,
            known_params - set(positional) - set(kwonly),
            "are supplied externally (e.g. by @velox.parametrize) but match none of this "
            "function's parameters -- check for a typo.",
        )

    n_defaulted = len(func.__defaults__ or ())
    required_positional = positional[: len(positional) - n_defaulted]
    missing = [
        p
        for p in required_positional
        if p not in injected and p not in known_params and p not in supplied and p != "self"
    ]

    kwdefaults = func.__kwdefaults__ or {}
    missing += [
        p for p in kwonly if p not in kwdefaults and p not in injected and p not in known_params
    ]

    if missing:
        params = ", ".join(missing)
        raise DIError(
            f"{name}: parameter(s) {params} have no default and are not injected via "
            f"Depends(...) -- velox has no name-based fixture lookup, so an uninjected "
            f"parameter can never be supplied."
        )
