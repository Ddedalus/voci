"""Injection declared as `db: Annotated[Session, Depends(db_fx)]` rather than as a default.

Deliberately **without** `from __future__ import annotations`: the annotations written in this
file are real objects, which is the path velox takes when it can. The other path — annotations
that arrive as source text, either from PEP 563 or from 3.14's lazy evaluation — is exercised
against modules built by `_module` below, since whether a module stringifies its annotations is a
property of its source that no test can set after the fact.
"""

import functools
from collections.abc import Callable
from types import ModuleType
from typing import Annotated

import pytest

import velox
from velox import Depends
from velox._di.fixtures import DIError, Injection, _check_missing_injections, plan_of


@velox.fixture()
def alpha() -> int:
    return 1


@velox.fixture()
def beta() -> int:
    return 2


#: Aliases are declared at module level because that is where velox can find one: an annotation
#: that arrives as source text is resolved against module globals, so a `type` statement inside
#: the function under test is reachable on 3.13 (where the annotation is an object) and not on
#: 3.14 (where it is a string). The reference documents the same limit for the fixture itself.
type Alpha = Annotated[int, Depends(alpha)]
type AlphaHandle = Alpha


def _module(source: str, name: str = "velox_annotated_probe") -> ModuleType:
    """`source` compiled and executed as a real module, so its functions get a real
    `__globals__` — which is the only namespace a stringified annotation can be resolved in."""
    module = ModuleType(name)
    module.__dict__["__file__"] = f"<{name}>"
    exec(compile(source, f"<{name}>", "exec"), module.__dict__)
    return module


_STRINGIFIED = """
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import velox
from velox import Depends

if TYPE_CHECKING:
    from decimal import Decimal


@velox.fixture()
def db() -> int:
    return 1


type Db = Annotated[int, Depends(db)]

MARKER = Depends(db)
"""


# --------------------------------------------------------------------------------------
# The object path: annotations that are objects by the time velox looks
# --------------------------------------------------------------------------------------


def test_a_marker_in_metadata_injects() -> None:
    def probe(db: Annotated[int, Depends(alpha)]) -> int:
        return db

    assert plan_of(probe) == (Injection(param="db", source=alpha, keyword_only=False),)


def test_a_fixture_can_declare_its_own_dependencies_the_same_way() -> None:
    @velox.fixture()
    def child(db: Annotated[int, Depends(alpha)]) -> int:
        return db + 1

    assert child.plan == (Injection(param="db", source=alpha, keyword_only=False),)


def test_a_keyword_only_parameter_keeps_its_flag() -> None:
    def probe(*, db: Annotated[int, Depends(alpha)]) -> int:
        return db

    assert plan_of(probe) == (Injection(param="db", source=alpha, keyword_only=True),)


def test_the_two_spellings_interleave_in_declaration_order() -> None:
    """One walk over the signature, so the order is the signature's — not defaults first."""

    def probe(
        first: Annotated[int, Depends(alpha)],
        second: int = Depends(beta),
        *,
        third: Annotated[int, Depends(alpha)],
        fourth: int = Depends(beta),
    ) -> int:
        return first + second + third + fourth

    assert [injection.param for injection in plan_of(probe)] == [
        "first",
        "second",
        "third",
        "fourth",
    ]


def test_metadata_that_is_not_a_marker_is_left_alone() -> None:
    def probe(db: Annotated[int, "documentation", 3] = 0) -> int:
        return db

    assert plan_of(probe) == ()


def test_an_ordinary_annotation_injects_nothing() -> None:
    def probe(db: int = 0) -> int:
        return db

    assert plan_of(probe) == ()


def test_an_alias_carries_the_marker() -> None:
    def probe(db: Alpha) -> int:
        return db

    assert plan_of(probe) == (Injection(param="db", source=alpha, keyword_only=False),)


def test_an_alias_of_an_alias_carries_it_too() -> None:
    def probe(db: AlphaHandle) -> int:
        return db

    assert plan_of(probe) == (Injection(param="db", source=alpha, keyword_only=False),)


def test_a_self_referential_alias_terminates() -> None:
    """`type A = A` hands back the alias object itself, so following `__value__` is bounded."""
    module = _module("type Loop = Loop\n\n\ndef probe(db: Loop) -> int:\n    return db\n")

    assert plan_of(module.probe) == ()


# --------------------------------------------------------------------------------------
# Rejections
# --------------------------------------------------------------------------------------


def test_declaring_both_spellings_for_one_parameter_is_an_error() -> None:
    def probe(db: Annotated[int, Depends(alpha)] = Depends(alpha)) -> int:
        return db

    with pytest.raises(DIError, match="declared twice"):
        plan_of(probe)


def test_two_markers_in_one_annotation_are_an_error() -> None:
    def probe(db: Annotated[int, Depends(alpha), Depends(beta)]) -> int:
        return db

    with pytest.raises(DIError, match="2 Depends"):
        plan_of(probe)


def test_a_positional_only_parameter_is_rejected_in_either_spelling() -> None:
    def probe(db: Annotated[int, Depends(alpha)], /) -> int:
        return db

    with pytest.raises(DIError, match="positional-only"):
        plan_of(probe)


def test_a_marker_on_a_name_that_is_not_a_parameter_is_an_error() -> None:
    """Nothing would ever be injected for it, and nothing else would say so. Reachable through
    `functools.wraps`, which copies the annotations of a function whose parameters it drops."""

    def wrapped(db: Annotated[int, Depends(alpha)]) -> int:
        return db

    @functools.wraps(wrapped)
    def probe() -> int:
        return 0

    with pytest.raises(DIError, match="not a parameter"):
        plan_of(probe)


def test_a_wraps_wrapper_reads_as_no_injections() -> None:
    """`functools.wraps` copies `__annotations__` but not the parameters they describe, so a
    wrapper presenting `(*args, **kwargs)` must not read as carrying its wrappee's injections —
    the existing sharp edge (a signature-replacing decorator hides them) rather than a new
    false positive. Collection reads the plan off the function underneath instead."""

    def announce[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return fn(*args, **kwargs)

        return wrapper

    def probe(db: Annotated[int, Depends(alpha)]) -> int:
        return db

    assert plan_of(announce(probe)) == ()


# --------------------------------------------------------------------------------------
# The source-text path: PEP 563 modules, and 3.14's lazy annotations
# --------------------------------------------------------------------------------------


def test_a_stringified_annotation_still_injects() -> None:
    module = _module(
        _STRINGIFIED + "\n\ndef probe(db: Annotated[int, Depends(db)]) -> int:\n    return db\n"
    )

    assert plan_of(module.probe) == (Injection(param="db", source=module.db, keyword_only=False),)


def test_a_stringified_alias_still_injects() -> None:
    module = _module(_STRINGIFIED + "\n\ndef probe(db: Db) -> int:\n    return db\n")

    assert plan_of(module.probe) == (Injection(param="db", source=module.db, keyword_only=False),)


def test_a_marker_held_in_a_module_level_name_is_resolved_by_lookup() -> None:
    """`Annotated[int, MARKER]` for a module-level `MARKER = Depends(db)`: a name, resolved the
    same way an alias is, never evaluated."""
    module = _module(
        _STRINGIFIED + "\n\ndef probe(db: Annotated[int, MARKER]) -> int:\n    return db\n"
    )

    assert plan_of(module.probe) == (Injection(param="db", source=module.db, keyword_only=False),)


def test_the_type_half_never_has_to_resolve() -> None:
    """A `TYPE_CHECKING`-only type is the whole point of parsing rather than evaluating."""
    module = _module(
        _STRINGIFIED + "\n\ndef probe(db: Annotated[Decimal, Depends(db)]) -> int:\n    return db\n"
    )

    assert plan_of(module.probe) == (Injection(param="db", source=module.db, keyword_only=False),)


def test_an_unrelated_unresolvable_annotation_does_not_stop_collection() -> None:
    module = _module(
        _STRINGIFIED + "\n\ndef probe(other: Decimal, db: Annotated[int, Depends(db)]) -> int:\n"
        "    return db\n"
    )

    assert plan_of(module.probe) == (Injection(param="db", source=module.db, keyword_only=False),)


def test_a_metadata_element_that_is_not_a_marker_is_never_evaluated() -> None:
    """velox compiles and evaluates exactly the `Depends(...)` calls it finds. Anything else in
    metadata — here a call that would raise — is left as text."""
    module = _module(
        _STRINGIFIED + "\n\ndef boom() -> int:\n"
        "    raise AssertionError('velox evaluated an annotation it should not have')\n\n\n"
        "def probe(db: Annotated[int, boom()] = 0) -> int:\n    return db\n"
    )

    assert plan_of(module.probe) == ()


def test_a_fixture_a_stringified_annotation_cannot_see_is_a_named_error() -> None:
    """The documented sharp edge: a string annotation is evaluated in module globals, so a
    fixture held in a local variable is unreachable. Better a `DIError` naming the parameter
    than a missing-argument failure later that names no annotation."""
    module = _module(
        _STRINGIFIED + "\n\n"
        "def build() -> object:\n"
        "    local = velox.fixture()(lambda: 1)\n\n"
        "    def probe(db: Annotated[int, Depends(local)]) -> int:\n"
        "        return db\n\n"
        "    return probe\n"
    )

    with pytest.raises(DIError, match="could not be evaluated"):
        plan_of(module.build())


def test_annotated_imported_only_for_type_checking_still_reads() -> None:
    """A stringifying module may import `Annotated` itself under `TYPE_CHECKING`. The spelling
    is enough to recognize the subscript; the marker inside it still has to resolve."""
    module = _module(
        "from __future__ import annotations\n\n"
        "from typing import TYPE_CHECKING\n\n"
        "import velox\n"
        "from velox import Depends\n\n"
        "if TYPE_CHECKING:\n"
        "    from typing import Annotated\n\n\n"
        "@velox.fixture()\n"
        "def db() -> int:\n"
        "    return 1\n\n\n"
        "def probe(value: Annotated[int, Depends(db)]) -> int:\n"
        "    return value\n"
    )

    assert plan_of(module.probe) == (
        Injection(param="value", source=module.db, keyword_only=False),
    )


# --------------------------------------------------------------------------------------
# What the rest of the machinery makes of a parameter with no default
# --------------------------------------------------------------------------------------


def test_an_injection_may_precede_a_parametrized_parameter() -> None:
    """The shape the default-position spelling could never produce: an injected parameter with
    no default, followed by one supplied externally. `wiring.py`'s reordering exists because of
    the constraint this drops."""

    def probe(db: Annotated[int, Depends(alpha)], case: int) -> int:
        return db + case

    _check_missing_injections(probe, plan_of(probe), known_params=frozenset({"case"}))


def test_a_parametrized_fixtures_param_still_reads_as_supplied() -> None:
    @velox.fixture(params=[1, 2])
    def cases(param: int, db: Annotated[int, Depends(alpha)]) -> int:
        return param + db

    assert cases.plan == (Injection(param="db", source=alpha, keyword_only=False),)


def test_an_injection_in_a_decorator_filled_slot_is_still_rejected() -> None:
    """`@mock.patch`'s mocks arrive first and positionally, so an injection there could never
    receive its value — the same rejection either spelling earns."""

    def probe(mock_thing: int, db: Annotated[int, Depends(alpha)]) -> int:
        return db

    with pytest.raises(DIError, match="the decorator wrapping this test fills itself"):
        _check_missing_injections(probe, plan_of(probe), positional_supplied=2)


def test_an_uninjected_required_parameter_is_still_reported() -> None:
    def probe(db: Annotated[int, Depends(alpha)], other: int) -> int:
        return db + other

    with pytest.raises(DIError, match="other"):
        _check_missing_injections(probe, plan_of(probe))
