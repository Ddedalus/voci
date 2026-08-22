"""Tests for velox_migrate.convert.rules: what each numbered rule writes, and what it declines.

Every rewrite is asserted as whole-module before and after source, because the diff a human reads
is the product: a test that only checked the rewritten line would pass while a rule silently
reflowed a call, moved a comment or requoted a string somewhere else in the file. Each rule is also
run twice over its own output, since a rule that matches what it just wrote would make `convert`
unrepeatable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

import libcst as cst
import pytest

from velox_migrate.convert import rules
from velox_migrate.convert.parametrize import Generated

PATH = "tests/test_suite.py"

BY_CODE = {rule.code: rule for rule in rules.RULES}


def _context(
    *tests: str,
    blocked: Iterable[str] = (),
    axis_ids: Mapping[tuple[str, str], tuple[str, ...]] | None = None,
    xfail_strict: bool = False,
    requested: Mapping[str, Mapping[str, str]] | None = None,
    finalizers: Iterable[str] = (),
    indirect: Mapping[str, frozenset[str]] | None = None,
    generated: Mapping[str, Sequence[Generated]] | None = None,
) -> rules.Context:
    return rules.Context(
        path=PATH,
        axis_ids=axis_ids or {},
        tests=frozenset(tests),
        blocked=frozenset(blocked),
        xfail_strict=xfail_strict,
        requested=requested or {},
        finalizers=frozenset(finalizers),
        indirect=indirect or {},
        generated=generated or {},
    )


def _apply(code: str, source: str, context: rules.Context) -> rules.Result:
    return BY_CODE[code].apply(cst.parse_module(source), context)


def _all(source: str, context: rules.Context) -> rules.Result:
    module = cst.parse_module(source)
    applied: list[rules.Applied] = []
    for rule in rules.RULES:
        result = rule.apply(module, context)
        module = result.module
        applied.extend(result.applied)
    return rules.Result(module=module, applied=tuple(applied))


def _rewrite(
    code: str, before: str, after: str, context: rules.Context
) -> tuple[rules.Applied, ...]:
    """Assert one rule turns `before` into `after`, and that a second pass changes nothing."""
    result = _apply(code, before, context)
    assert result.module.code == after
    again = _apply(code, after, context)
    assert again.module.code == after
    return result.applied


def _untouched(code: str, source: str, context: rules.Context) -> tuple[rules.Applied, ...]:
    """Assert one rule leaves `source` byte-identical, and return what it recorded about it."""
    result = _apply(code, source, context)
    assert result.module.code == source
    return result.applied


def _codes(applied: Iterable[rules.Applied]) -> list[str]:
    return [line.code for line in applied]


# --- the registry ------------------------------------------------------------------------------


def test_the_registry_is_in_code_order() -> None:
    codes = [rule.code for rule in rules.RULES]

    assert codes == sorted(codes)


def test_every_rule_names_a_support_matrix_row() -> None:
    from velox_migrate import matrix

    for rule in rules.RULES:
        assert rule.code in matrix.BY_CODE


def test_a_disabled_code_is_left_out_of_the_set() -> None:
    remaining = rules.enabled(["VX101", "vx204 "])

    assert [rule.code for rule in remaining] == [
        code for code in (rule.code for rule in rules.RULES) if code not in ("VX101", "VX204")
    ]


def test_disabling_nothing_is_the_whole_set() -> None:
    assert rules.enabled(()) == rules.RULES


# --- VX009 usefixtures -------------------------------------------------------------------------


def test_a_usefixtures_mark_goes_away() -> None:
    # The fixture it named is declared with `velox.use(...)` on the module, which is placed from
    # the dump rather than from this mark -- all the rule has to do is take the mark away.
    before = """import pytest


@pytest.mark.usefixtures("engine")
def test_x():
    pass
"""
    after = """import pytest


def test_x():
    pass
"""
    applied = _rewrite("VX009", before, after, _context("test_x"))

    assert _codes(applied) == ["VX009"]


def test_a_usefixtures_mark_on_a_refused_test_stays_with_it() -> None:
    source = """import pytest


@pytest.mark.usefixtures("engine")
def test_x():
    pass
"""
    assert _untouched("VX009", source, _context("test_x", blocked=["test_x"])) == ()


# --- VX101 parametrize -------------------------------------------------------------------------


def test_parametrize_carries_the_dumps_own_ids() -> None:
    before = """import pytest


@pytest.mark.parametrize("value", [1, 2])
def test_x(value):
    assert value
"""
    after = """import pytest


@velox.parametrize("value", [1, 2], ids=("1", "2"))
def test_x(value):
    assert value
"""
    applied = _rewrite(
        "VX101",
        before,
        after,
        _context("test_x", axis_ids={("test_x", "value"): ("1", "2")}),
    )

    assert _codes(applied) == ["VX101"]


def test_parametrize_keeps_a_call_written_over_several_lines() -> None:
    before = """import pytest


@pytest.mark.parametrize(
    "a,b",
    [
        (1, 2),  # the first case
        (3, 4),
    ],
)
def test_x(a, b):
    assert a < b
"""
    after = """import pytest


@velox.parametrize(
    "a,b",
    [
        (1, 2),  # the first case
        (3, 4),
    ],
    ids=("1-2", "3-4"),
)
def test_x(a, b):
    assert a < b
"""
    _rewrite(
        "VX101",
        before,
        after,
        _context("test_x", axis_ids={("test_x", "a,b"): ("1-2", "3-4")}),
    )


def test_parametrize_unwraps_a_param_and_folds_its_id() -> None:
    before = """import pytest


@pytest.mark.parametrize("value", [pytest.param(1, id="one"), pytest.param(2, id="two")])
def test_x(value):
    assert value
"""
    after = """import pytest


@velox.parametrize("value", [1, 2], ids=("one", "two"))
def test_x(value):
    assert value
"""
    _rewrite("VX101", before, after, _context("test_x"))


def test_parametrize_unwraps_a_multi_value_param_into_a_tuple() -> None:
    before = """import pytest


@pytest.mark.parametrize("a,b", [pytest.param(1, 2, id="low")])
def test_x(a, b):
    assert a < b
"""
    after = """import pytest


@velox.parametrize("a,b", [(1, 2)], ids=("low",))
def test_x(a, b):
    assert a < b
"""
    _rewrite("VX101", before, after, _context("test_x"))


def test_parametrize_keeps_the_sources_own_ids_when_the_dump_has_none() -> None:
    before = """import pytest


@pytest.mark.parametrize("value", [1, 2], ids=["one", "two"])
def test_x(value):
    assert value
"""
    after = """import pytest


@velox.parametrize("value", [1, 2], ids=["one", "two"])
def test_x(value):
    assert value
"""
    _rewrite("VX101", before, after, _context("test_x"))


def test_parametrize_without_attributable_ids_is_the_case_ids_row() -> None:
    before = """import pytest


@pytest.mark.parametrize("value", [1, 2])
def test_x(value):
    assert value
"""
    after = """import pytest


@velox.parametrize("value", [1, 2])
def test_x(value):
    assert value
"""
    applied = _rewrite("VX101", before, after, _context("test_x"))

    assert _codes(applied) == ["VX114"]


def test_parametrize_ignores_ids_the_dump_and_the_source_disagree_on() -> None:
    source = """import pytest


@pytest.mark.parametrize("value", [1, 2])
def test_x(value):
    assert value
"""
    result = _apply(
        "VX101", source, _context("test_x", axis_ids={("test_x", "value"): ("1", "2", "3")})
    )

    assert "ids=" not in result.module.code


def test_a_mark_on_one_case_becomes_velox_case() -> None:
    before = """import pytest


@pytest.mark.parametrize("value", [1, pytest.param(2, marks=pytest.mark.skip)])
def test_x(value):
    assert value
"""
    after = """import pytest


@velox.parametrize("value", [1, velox.case(2, marks=velox.skip("no reason given"))])
def test_x(value):
    assert value
"""
    applied = _rewrite("VX101", before, after, _context("test_x"))

    assert _codes(applied) == ["VX102"]


def test_several_marks_on_one_case_become_a_marks_list() -> None:
    before = """import pytest


@pytest.mark.parametrize(
    "value", [pytest.param(2, marks=[pytest.mark.xfail(reason="known"), pytest.mark.slow])]
)
def test_x(value):
    assert value
"""
    after = """import pytest


@velox.parametrize(
    "value", [velox.case(2, marks=[velox.xfail("known"), velox.tag("slow")])]
)
def test_x(value):
    assert value
"""
    applied = _rewrite("VX101", before, after, _context("test_x"))

    assert _codes(applied) == ["VX102"]


def test_a_cases_xfail_mark_carries_the_suites_strict() -> None:
    """A case's own `xfail` reads `strict=` from the suite's `xfail_strict` ini setting exactly
    as a top-level `@velox.xfail` does -- `_StrictXFail` (VX107) only patches decorators, so a
    mark nested inside `velox.case(..., marks=...)` must already carry it."""
    before = """import pytest


@pytest.mark.parametrize("value", [pytest.param(2, marks=pytest.mark.xfail(reason="known"))])
def test_x(value):
    assert value
"""
    after = """import pytest


@velox.parametrize("value", [velox.case(2, marks=velox.xfail("known", strict=True))])
def test_x(value):
    assert value
"""
    applied = _rewrite("VX101", before, after, _context("test_x", xfail_strict=True))

    assert _codes(applied) == ["VX102"]


def test_a_case_marked_with_several_values_spells_them_out() -> None:
    before = """import pytest


@pytest.mark.parametrize("a,b", [pytest.param(1, 2, marks=pytest.mark.skip)])
def test_x(a, b):
    assert a < b
"""
    after = """import pytest


@velox.parametrize("a,b", [velox.case(1, 2, marks=velox.skip("no reason given"))])
def test_x(a, b):
    assert a < b
"""
    _rewrite("VX101", before, after, _context("test_x"))


def test_a_case_mark_and_its_id_both_carry() -> None:
    before = """import pytest


@pytest.mark.parametrize("value", [pytest.param(2, marks=pytest.mark.skip, id="two")])
def test_x(value):
    assert value
"""
    after = """import pytest


@velox.parametrize("value", [velox.case(2, marks=velox.skip("no reason given"))], ids=("two",))
def test_x(value):
    assert value
"""
    _rewrite("VX101", before, after, _context("test_x"))


def test_an_asyncio_case_mark_carries_nothing() -> None:
    """`@pytest.mark.asyncio` goes away wherever it is written -- on a case, the case is left as
    the bare value it would have been with no `marks=` at all."""
    before = """import pytest


@pytest.mark.parametrize("value", [pytest.param(2, marks=pytest.mark.asyncio)])
def test_x(value):
    assert value
"""
    after = """import pytest


@velox.parametrize("value", [2])
def test_x(value):
    assert value
"""
    applied = _rewrite("VX101", before, after, _context("test_x"))

    assert _codes(applied) == ["VX114"]


def test_a_plugin_mark_on_one_case_refuses_the_whole_parametrize() -> None:
    source = """import pytest


@pytest.mark.parametrize("value", [1, pytest.param(2, marks=pytest.mark.filterwarnings("error"))])
def test_x(value):
    assert value
"""
    applied = _untouched("VX101", source, _context("test_x"))

    assert _codes(applied) == ["VX102"]


def test_two_scalar_marks_on_one_case_refuse_the_whole_parametrize() -> None:
    source = """import pytest


@pytest.mark.parametrize(
    "value", [pytest.param(2, marks=[pytest.mark.skip, pytest.mark.skip(reason="again")])]
)
def test_x(value):
    assert value
"""
    applied = _untouched("VX101", source, _context("test_x"))

    assert _codes(applied) == ["VX113"]


def test_an_indirect_parametrize_the_plan_did_not_list_stays_where_it_was() -> None:
    # Whether the fixture can carry these values is a question about every test in the suite that
    # reaches it, so a rule told nothing about it writes nothing.
    source = """import pytest


@pytest.mark.parametrize("value", [1, 2], indirect=True)
def test_x(value):
    assert value
"""
    applied = _untouched("VX101", source, _context("test_x"))

    assert _codes(applied) == ["VX029"]


def test_an_indirect_parametrize_the_plan_listed_goes_away() -> None:
    # The values are written onto the fixture by the wiring swap, so what is left here is a mark
    # with nothing to become.
    before = """import pytest


@pytest.mark.parametrize("value", [1, 2], indirect=True)
def test_x(value):
    assert value
"""
    after = """import pytest


def test_x(value):
    assert value
"""
    applied = _rewrite(
        "VX007", before, after, _context("test_x", indirect={"test_x": frozenset({"value"})})
    )

    assert _codes(applied) == ["VX007"]


def test_the_cases_a_hook_produced_are_written_out_with_pytests_own_ids() -> None:
    before = """def test_x(width, height):
    assert width * height
"""
    after = """@velox.parametrize("width,height", [(2, 3), (5, 8)], ids=["small", "large"])
def test_x(width, height):
    assert width * height
"""
    generated = {
        "test_x": [
            Generated(
                argnames=("width", "height"),
                values=(("2", "3"), ("5", "8")),
                ids=("small", "large"),
            )
        ]
    }
    applied = _rewrite("VX024", before, after, _context("test_x", generated=generated))

    assert _codes(applied) == ["VX024"]


def test_a_generated_axis_whose_ids_no_one_position_explains_says_so() -> None:
    before = """def test_x(letter):
    assert letter
"""
    after = """@velox.parametrize("letter", ["a", "b"])
def test_x(letter):
    assert letter
"""
    generated = {"test_x": [Generated(argnames=("letter",), values=(("'a'",), ("'b'",)), ids=None)]}
    applied = _rewrite("VX024", before, after, _context("test_x", generated=generated))

    assert _codes(applied) == ["VX114"]


def test_stacked_parametrize_marks_are_reversed() -> None:
    # pytest applies decorators bottom-up, so `b` is registered first, varies slowest and is
    # written first in the case id; velox reads its own list outermost-first. Reversing is what
    # keeps `test_x[3-1]` where pytest put it, and the case product is the same either way.
    before = """import pytest


@pytest.mark.parametrize("a", [1, 2])
@pytest.mark.parametrize("b", [3, 4])
def test_x(a, b):
    assert a < b
"""
    after = """import pytest


@velox.parametrize("b", [3, 4], ids=("3", "4"))
@velox.parametrize("a", [1, 2], ids=("1", "2"))
def test_x(a, b):
    assert a < b
"""
    _rewrite(
        "VX101",
        before,
        after,
        _context(
            "test_x",
            axis_ids={("test_x", "a"): ("1", "2"), ("test_x", "b"): ("3", "4")},
        ),
    )


# --- VX103 string conditions -------------------------------------------------------------------


def test_a_string_skipif_condition_becomes_a_lambda() -> None:
    before = """import pytest


@pytest.mark.skipif("sys.platform == 'win32'", reason="posix only")
def test_x():
    pass
"""
    after = """import pytest


@velox.skipif(lambda: sys.platform == 'win32', reason="posix only")
def test_x():
    pass
"""
    applied = _rewrite("VX103", before, after, _context("test_x"))

    assert _codes(applied) == ["VX103"]
    assert "`sys`" in applied[0].message


def test_a_string_condition_reading_pytests_config_is_refused() -> None:
    source = """import pytest


@pytest.mark.skipif("config.getoption('slow')", reason="slow")
def test_x():
    pass
"""
    applied = _untouched("VX103", source, _context("test_x"))

    assert _codes(applied) == ["VX103"]
    assert "config" in applied[0].message


def test_a_string_condition_that_is_not_an_expression_is_refused() -> None:
    source = """import pytest


@pytest.mark.skipif("not a expression =", reason="broken")
def test_x():
    pass
"""
    applied = _untouched("VX103", source, _context("test_x"))

    assert _codes(applied) == ["VX103"]


# --- VX104 skip, skipif, xfail -----------------------------------------------------------------


def test_a_bare_skip_gets_the_reason_velox_requires() -> None:
    before = """import pytest


@pytest.mark.skip
def test_x():
    pass
"""
    after = """import pytest


@velox.skip("no reason given")
def test_x():
    pass
"""
    applied = _rewrite("VX104", before, after, _context("test_x"))

    assert _codes(applied) == ["VX104"]


def test_a_skip_reason_becomes_positional() -> None:
    before = """import pytest


@pytest.mark.skip(reason="flaky")
def test_x():
    pass
"""
    after = """import pytest


@velox.skip("flaky")
def test_x():
    pass
"""
    _rewrite("VX104", before, after, _context("test_x"))


def test_a_skipif_reason_becomes_keyword_only() -> None:
    before = """import pytest


@pytest.mark.skipif(sys.version_info < (3, 14), "too old")
def test_x():
    pass
"""
    after = """import pytest


@velox.skipif(sys.version_info < (3, 14), reason="too old")
def test_x():
    pass
"""
    _rewrite("VX104", before, after, _context("test_x"))


def test_a_plain_xfail_keeps_its_strict_and_raises() -> None:
    before = """import pytest


@pytest.mark.xfail(reason="broken", strict=True, raises=ValueError)
def test_x():
    pass
"""
    after = """import pytest


@velox.xfail("broken", strict=True, raises=ValueError)
def test_x():
    pass
"""
    _rewrite("VX104", before, after, _context("test_x"))


def test_a_conditional_xfail_becomes_condition() -> None:
    before = """import pytest


@pytest.mark.xfail(sys.platform == "win32", reason="broken")
def test_x():
    pass
"""
    after = """import pytest


@velox.xfail("broken", condition=sys.platform == "win32")
def test_x():
    pass
"""
    applied = _rewrite("VX104", before, after, _context("test_x"))

    assert _codes(applied) == ["VX105"]


def test_a_conditional_xfail_positional_becomes_condition() -> None:
    before = """import pytest


@pytest.mark.xfail(sys.platform == "win32", reason="broken", strict=True)
def test_x():
    pass
"""
    after = """import pytest


@velox.xfail("broken", condition=sys.platform == "win32", strict=True)
def test_x():
    pass
"""
    applied = _rewrite("VX104", before, after, _context("test_x"))

    assert _codes(applied) == ["VX105"]


def test_a_string_xfail_condition_becomes_a_lambda() -> None:
    before = """import pytest


@pytest.mark.xfail("sys.platform == 'win32'", reason="broken")
def test_x():
    pass
"""
    after = """import pytest


@velox.xfail("broken", condition=lambda: sys.platform == 'win32')
def test_x():
    pass
"""
    applied = _rewrite("VX116", before, after, _context("test_x"))

    assert _codes(applied) == ["VX116"]
    assert "`sys`" in applied[0].message


def test_a_string_xfail_condition_reading_pytests_config_is_refused() -> None:
    source = """import pytest


@pytest.mark.xfail("config.getoption('slow')", reason="slow")
def test_x():
    pass
"""
    applied = _untouched("VX116", source, _context("test_x"))

    assert _codes(applied) == ["VX116"]
    assert "config" in applied[0].message


def test_an_unrun_conditional_xfail_is_refused() -> None:
    """`run=False` becomes a skip, which has no condition of its own -- applying it always would
    not be the conditional expectation the source wrote."""
    source = """import pytest


@pytest.mark.xfail(run=False, reason="segfaults", condition=slow)
def test_x():
    pass
"""
    applied = _untouched("VX106", source, _context("test_x"))

    assert _codes(applied) == ["VX106"]


def test_two_skips_on_one_test_are_refused_rather_than_stacked() -> None:
    source = """import pytest


@pytest.mark.skip(reason="one")
@pytest.mark.skip(reason="two")
def test_x():
    pass
"""
    applied = _untouched("VX104", source, _context("test_x"))

    assert _codes(applied) == ["VX113", "VX113"]


def test_a_mark_imported_from_pytest_by_name_is_the_same_mark() -> None:
    before = """from pytest import mark


@mark.skip
def test_x():
    pass
"""
    after = """from pytest import mark


@velox.skip("no reason given")
def test_x():
    pass
"""
    _rewrite("VX104", before, after, _context("test_x"))


def test_two_skipifs_on_one_test_stack_freely() -> None:
    before = """import pytest


@pytest.mark.skipif(slow, reason="slow")
@pytest.mark.skipif(windows, reason="windows")
def test_x():
    pass
"""
    after = """import pytest


@velox.skipif(slow, reason="slow")
@velox.skipif(windows, reason="windows")
def test_x():
    pass
"""
    _rewrite("VX104", before, after, _context("test_x"))


# --- VX106 xfail(run=False) --------------------------------------------------------------------


def test_an_xfail_that_never_runs_becomes_a_skip() -> None:
    before = """import pytest


@pytest.mark.xfail(run=False, reason="segfaults")
def test_x():
    pass
"""
    after = """import pytest


@velox.skip("segfaults")
def test_x():
    pass
"""
    applied = _rewrite("VX106", before, after, _context("test_x"))

    assert _codes(applied) == ["VX106"]


def test_the_skip_and_xfail_rules_leave_each_others_marks_alone() -> None:
    source = """import pytest


@pytest.mark.xfail(run=False, reason="segfaults")
def test_x():
    pass
"""
    assert _untouched("VX104", source, _context("test_x")) == ()


# --- VX107 xfail_strict -----------------------------------------------------------------------


def test_the_suites_xfail_strict_is_written_into_a_generated_xfail() -> None:
    before = """import pytest


@pytest.mark.xfail(reason="broken")
def test_x():
    pass
"""
    after = """import pytest


@velox.xfail("broken", strict=True)
def test_x():
    pass
"""
    result = _all(before, _context("test_x", xfail_strict=True))

    assert result.module.code == after
    assert _codes(result.applied) == ["VX104", "VX107"]
    assert _all(after, _context("test_x", xfail_strict=True)).module.code == after


def test_an_explicit_strict_is_left_as_the_source_wrote_it() -> None:
    source = """import pytest


@velox.xfail("broken", strict=False)
def test_x():
    pass
"""
    assert _untouched("VX107", source, _context("test_x", xfail_strict=True)) == ()


# --- VX109 custom marks -----------------------------------------------------------------------


def test_a_custom_mark_becomes_a_tag() -> None:
    before = """import pytest


@pytest.mark.slow
def test_x():
    pass
"""
    after = """import pytest


@velox.tag("slow")
def test_x():
    pass
"""
    applied = _rewrite("VX109", before, after, _context("test_x"))

    assert _codes(applied) == ["VX109"]


def test_a_custom_marks_arguments_are_dropped() -> None:
    before = """import pytest


@pytest.mark.tier(2, note="nightly")
def test_x():
    pass
"""
    after = """import pytest


@velox.tag("tier")
def test_x():
    pass
"""
    applied = _rewrite("VX109", before, after, _context("test_x"))

    assert "dropped" in applied[0].message


def test_a_plugins_mark_is_not_a_tag() -> None:
    source = """import pytest


@pytest.mark.django_db
def test_x():
    pass
"""
    assert _untouched("VX109", source, _context("test_x")) == ()


def test_usefixtures_is_left_to_the_wiring() -> None:
    source = """import pytest


@pytest.mark.usefixtures("engine")
def test_x():
    pass
"""
    assert _untouched("VX109", source, _context("test_x")) == ()


# --- VX110 async marks ------------------------------------------------------------------------


def test_an_asyncio_mark_goes_away() -> None:
    before = """import pytest


@pytest.mark.asyncio
async def test_x():
    pass
"""
    after = """import pytest


async def test_x():
    pass
"""
    applied = _rewrite("VX110", before, after, _context("test_x"))

    assert _codes(applied) == ["VX110"]


def test_deleting_a_mark_keeps_the_comment_written_under_it() -> None:
    before = """import pytest


@pytest.mark.anyio
# the tag matters
@pytest.mark.slow
async def test_x():
    pass
"""
    after = """import pytest


# the tag matters
@pytest.mark.slow
async def test_x():
    pass
"""
    _rewrite("VX110", before, after, _context("test_x"))


def test_deleting_the_last_mark_keeps_the_comment_above_the_def() -> None:
    before = """import pytest


@pytest.mark.slow
# nothing else
@pytest.mark.asyncio
async def test_x():
    pass
"""
    after = """import pytest


@pytest.mark.slow
# nothing else
async def test_x():
    pass
"""
    _rewrite("VX110", before, after, _context("test_x"))


# --- VX111 timeout ----------------------------------------------------------------------------


def test_a_timeout_mark_becomes_velox_timeout() -> None:
    before = """import pytest


@pytest.mark.timeout(30)
def test_x():
    pass
"""
    after = """import pytest


@velox.timeout(30)
def test_x():
    pass
"""
    applied = _rewrite("VX111", before, after, _context("test_x"))

    assert _codes(applied) == ["VX111"]


def test_a_timeouts_other_arguments_are_dropped() -> None:
    before = """import pytest


@pytest.mark.timeout(timeout=30, method="thread")
def test_x():
    pass
"""
    after = """import pytest


@velox.timeout(30)
def test_x():
    pass
"""
    applied = _rewrite("VX111", before, after, _context("test_x"))

    assert "method" in applied[0].message


def test_a_timeout_of_zero_is_refused() -> None:
    source = """import pytest


@pytest.mark.timeout(0)
def test_x():
    pass
"""
    applied = _untouched("VX111", source, _context("test_x"))

    assert _codes(applied) == ["VX111"]
    assert "positive" in applied[0].message


# --- VX115 pytestmark -------------------------------------------------------------------------


def test_a_module_pytestmark_becomes_a_decorator_on_every_test() -> None:
    before = """import pytest

pytestmark = pytest.mark.slow


def test_x():
    pass


def test_y():
    pass
"""
    after = """import pytest


@velox.tag("slow")
def test_x():
    pass


@velox.tag("slow")
def test_y():
    pass
"""
    applied = _rewrite("VX115", before, after, _context("test_x", "test_y"))

    assert _codes(applied) == ["VX115"]


def test_a_class_pytestmark_lands_on_the_methods_not_the_class() -> None:
    before = """import pytest


class TestGroup:
    pytestmark = [pytest.mark.skip(reason="later")]

    def test_x(self):
        pass
"""
    after = """import pytest


class TestGroup:
    @velox.skip("later")
    def test_x(self):
        pass
"""
    _rewrite("VX115", before, after, _context("TestGroup.test_x"))


def test_a_mark_decorating_a_class_lands_on_the_methods() -> None:
    before = """import pytest


@pytest.mark.slow
class TestGroup:
    def test_x(self):
        pass
"""
    after = """import pytest


class TestGroup:
    @velox.tag("slow")
    def test_x(self):
        pass
"""
    _rewrite("VX115", before, after, _context("TestGroup.test_x"))


def test_a_pytestmark_comment_moves_with_the_marks() -> None:
    before = """import pytest

# everything here is slow
pytestmark = pytest.mark.slow


def test_x():
    pass
"""
    after = """import pytest


# everything here is slow
@velox.tag("slow")
def test_x():
    pass
"""
    _rewrite("VX115", before, after, _context("test_x"))


def test_a_pytestmark_holding_one_mark_no_rule_writes_stays_whole() -> None:
    source = """import pytest

pytestmark = [pytest.mark.slow, pytest.mark.filterwarnings("error")]


def test_x():
    pass
"""
    applied = _untouched("VX115", source, _context("test_x"))

    assert _codes(applied) == ["VX108"]


def test_a_pytestmark_reaching_no_converted_test_stays_where_it_is() -> None:
    source = """import pytest

pytestmark = pytest.mark.slow


def test_x():
    pass
"""
    applied = _untouched("VX115", source, _context("test_x", blocked=["test_x"]))

    assert _codes(applied) == ["VX115"]


def test_a_distributed_scalar_mark_is_not_stacked_on_a_test_that_has_one() -> None:
    before = """import pytest

pytestmark = pytest.mark.skip(reason="module")


@velox.skip("its own")
def test_x():
    pass


def test_y():
    pass
"""
    after = """import pytest


@velox.skip("its own")
def test_x():
    pass


@velox.skip("module")
def test_y():
    pass
"""
    applied = _rewrite("VX115", before, after, _context("test_x", "test_y"))

    assert _codes(applied) == ["VX115", "VX113"]


def test_a_module_and_a_class_pytestmark_both_reach_a_method() -> None:
    before = """import pytest

pytestmark = pytest.mark.slow


class TestGroup:
    pytestmark = [pytest.mark.timeout(3)]

    def test_x(self):
        pass
"""
    after = """import pytest


class TestGroup:
    @velox.tag("slow")
    @velox.timeout(3)
    def test_x(self):
        pass
"""
    _rewrite("VX115", before, after, _context("TestGroup.test_x"))


def test_a_distributed_xfail_carries_the_suites_strict() -> None:
    before = """import pytest

pytestmark = pytest.mark.xfail(reason="broken")


def test_x():
    pass
"""
    after = """import pytest


@velox.xfail("broken", strict=True)
def test_x():
    pass
"""
    context = _context("test_x", xfail_strict=True)
    result = _all(before, context)

    assert result.module.code == after
    assert _all(after, context).module.code == after


# --- VX011 getfixturevalue --------------------------------------------------------------------


def test_a_literal_getfixturevalue_becomes_the_name_of_the_parameter_it_injects() -> None:
    before = """def engine(request):
    return {"dsn": request.getfixturevalue("settings")["dsn"]}
"""
    after = """def engine(request):
    return {"dsn": settings["dsn"]}
"""
    applied = _rewrite(
        "VX011", before, after, _context(requested={"engine": {"settings": "settings"}})
    )

    assert _codes(applied) == ["VX011"]


def test_a_getfixturevalue_of_a_renamed_builtin_becomes_the_name_velox_binds() -> None:
    before = """def test_x(request):
    assert request.getfixturevalue("tmp_path").is_dir()
"""
    after = """def test_x(request):
    assert tmp_path.is_dir()
"""
    _rewrite(
        "VX011",
        before,
        after,
        _context("test_x", requested={"test_x": {"tmp_path": "tmp_path"}}),
    )


def test_a_getfixturevalue_the_plan_did_not_attribute_is_left_where_it_is() -> None:
    # The name may mean two fixtures, or the definition may be one nothing can grow a parameter
    # on. Either way the plan says so, and this rule writes only where it does.
    source = """def engine(request):
    return request.getfixturevalue("settings")
"""

    assert _untouched("VX011", source, _context()) == ()


# --- VX013 addfinalizer -----------------------------------------------------------------------


def test_an_unconditional_finalizer_becomes_the_teardown_after_a_yield() -> None:
    before = """def ledger(request):
    entries = []

    request.addfinalizer(entries.clear)
    return entries
"""
    after = """def ledger(request):
    entries = []

    yield entries
    entries.clear()
"""
    applied = _rewrite("VX013", before, after, _context(finalizers=["ledger"]))

    assert _codes(applied) == ["VX013"]


def test_finalizers_run_in_the_reverse_of_the_order_they_were_registered_in() -> None:
    # pytest runs them last-registered-first, and a `yield` fixture runs its teardown top to
    # bottom, so the order the calls are written in is the reverse of the order they were made in.
    before = """def journal(request, log):
    request.addfinalizer(lambda: log.append("outer"))
    request.addfinalizer(close)
    return {"open": True}
"""
    after = """def journal(request, log):
    yield {"open": True}
    close()
    (lambda: log.append("outer"))()
"""
    _rewrite("VX013", before, after, _context(finalizers=["journal"]))


def test_a_factory_that_hands_nothing_back_yields_nothing() -> None:
    before = """def audited(request, log):
    log.append("setup")
    request.addfinalizer(close)
"""
    after = """def audited(request, log):
    log.append("setup")
    yield
    close()
"""
    _rewrite("VX013", before, after, _context(finalizers=["audited"]))


def test_a_factory_ending_in_a_bare_return_yields_nothing_in_its_place() -> None:
    before = """def audited(request, log):
    log.append("setup")
    request.addfinalizer(close)
    return
"""
    after = """def audited(request, log):
    log.append("setup")
    yield
    close()
"""
    _rewrite("VX013", before, after, _context(finalizers=["audited"]))


def test_a_return_sharing_its_line_with_another_statement_still_becomes_the_yield() -> None:
    before = """def ledger(request):
    request.addfinalizer(close)
    entries = []; return entries
"""
    after = """def ledger(request):
    entries = []; yield entries
    close()
"""
    _rewrite("VX013", before, after, _context(finalizers=["ledger"]))


def test_a_finalizer_registered_inside_a_with_block_leaves_the_block_behind() -> None:
    before = """def handle(request):
    with open("f") as file:
        request.addfinalizer(file.close)
    return file
"""
    after = """def handle(request):
    with open("f") as file:
        pass
    yield file
    file.close()
"""
    _rewrite("VX013", before, after, _context(finalizers=["handle"]))


def test_a_registration_inside_a_nested_def_stays_where_it_was_written() -> None:
    # What a nested function registers is registered only when something calls it, which is not
    # the shape the plan attributes a site for — so the fixture around it is not one either.
    source = """def ledger(request):
    def register():
        request.addfinalizer(close)

    return register
"""

    assert _untouched("VX013", source, _context(finalizers=["ledger.register"])) == ()


# --- VX201 capsys -----------------------------------------------------------------------------


def test_a_readouterr_read_as_an_attribute_becomes_the_live_capture() -> None:
    before = """def test_x(capsys):
    print("hello")
    assert capsys.readouterr().out == "hello\\n"
"""
    after = """def test_x(capsys):
    print("hello")
    assert capture.out == "hello\\n"
"""
    applied = _rewrite("VX201", before, after, _context("test_x"))

    assert _codes(applied) == ["VX201"]


def test_a_readouterr_bound_to_a_name_becomes_the_capture_itself() -> None:
    before = """def test_x(capsys):
    captured = capsys.readouterr()
    assert captured.out
"""
    after = """def test_x(capsys):
    captured = capture
    assert captured.out
"""
    _rewrite("VX201", before, after, _context("test_x"))


def test_an_unpacked_readouterr_becomes_the_two_live_reads() -> None:
    before = """def test_x(capsys):
    out, err = capsys.readouterr()
    assert not err
"""
    after = """def test_x(capsys):
    out, err = capture.out, capture.err
    assert not err
"""
    _rewrite("VX201", before, after, _context("test_x"))


def test_a_readouterr_called_only_to_clear_the_buffer_is_refused() -> None:
    source = """def test_x(capsys):
    print("noise")
    capsys.readouterr()
    print("signal")
    assert capsys.readouterr().out == "signal\\n"
"""
    result = _apply("VX201", source, _context("test_x"))

    assert _codes(result.applied) == ["VX201", "VX201", "VX202"]
    assert "capsys.readouterr()\n" in result.module.code


def test_two_readouterr_reads_in_one_body_are_the_cumulative_row() -> None:
    before = """def test_x(capsys):
    assert capsys.readouterr().out == "a\\n"
    assert capsys.readouterr().out == "b\\n"
"""
    after = """def test_x(capsys):
    assert capture.out == "a\\n"
    assert capture.out == "b\\n"
"""
    applied = _rewrite("VX201", before, after, _context("test_x"))

    assert _codes(applied) == ["VX201", "VX201", "VX202"]


def test_a_local_called_capsys_is_not_the_fixture() -> None:
    source = """def test_x():
    capsys = FakeCapsys()
    assert capsys.readouterr().out
"""
    assert _untouched("VX201", source, _context("test_x")) == ()


def test_capsys_is_read_after_the_wiring_has_renamed_the_parameter() -> None:
    before = """def test_x(capture=Depends(velox.capture)):
    assert capture.readouterr().out == ""
"""
    after = """def test_x(capture=Depends(velox.capture)):
    assert capture.out == ""
"""
    _rewrite("VX201", before, after, _context("test_x"))


# --- VX204 caplog attributes ------------------------------------------------------------------


def test_caplog_records_and_messages_move_to_log_records() -> None:
    before = """def test_x(caplog):
    assert caplog.records == []
    assert caplog.messages == []
"""
    after = """def test_x(caplog):
    assert log_records.records == []
    assert log_records.messages == []
"""
    applied = _rewrite("VX204", before, after, _context("test_x"))

    assert _codes(applied) == ["VX204", "VX204"]


def test_caplog_text_and_record_tuples_move_to_log_records_under_vx206() -> None:
    before = """def test_x(caplog):
    assert caplog.text == ""
    assert caplog.record_tuples == []
"""
    after = """def test_x(caplog):
    assert log_records.text == ""
    assert log_records.record_tuples == []
"""
    applied = _rewrite("VX204", before, after, _context("test_x"))

    assert _codes(applied) == ["VX206", "VX206"]


def test_caplog_clear_call_moves_to_log_records_under_vx206() -> None:
    before = """def test_x(caplog):
    caplog.clear()
"""
    after = """def test_x(caplog):
    log_records.clear()
"""
    applied = _rewrite("VX204", before, after, _context("test_x"))

    assert _codes(applied) == ["VX206"]


def test_caplogs_handler_is_left_to_its_own_row() -> None:
    source = """def test_x(caplog):
    caplog.handler.flush()
"""
    assert _untouched("VX204", source, _context("test_x")) == ()


# --- VX205 caplog.set_level -------------------------------------------------------------------


def test_set_level_becomes_a_block_over_the_rest_of_the_test() -> None:
    before = """import logging


def test_x(caplog):
    caplog.set_level(logging.INFO)
    logging.info("hello")
    # the level is process-global
    assert caplog.messages == ["hello"]
"""
    after = """import logging


def test_x(caplog):
    with log_records.set_level(logging.INFO):
        logging.info("hello")
        # the level is process-global
        assert caplog.messages == ["hello"]
"""
    applied = _rewrite("VX205", before, after, _context("test_x"))

    assert _codes(applied) == ["VX205"]


def test_a_set_levels_second_argument_is_named() -> None:
    before = """def test_x(caplog):
    caplog.set_level(logging.INFO, "app.db")
    run()
"""
    after = """def test_x(caplog):
    with log_records.set_level(logging.INFO, logger="app.db"):
        run()
"""
    _rewrite("VX205", before, after, _context("test_x"))


def test_a_conditional_set_level_is_refused() -> None:
    source = """def test_x(caplog):
    if verbose:
        caplog.set_level(logging.INFO)
    run()
"""
    assert _untouched("VX205", source, _context("test_x")) == ()


def test_a_second_set_level_stays_inside_the_first_ones_block() -> None:
    before = """def test_x(caplog):
    caplog.set_level(logging.INFO)
    run()
    caplog.set_level(logging.DEBUG)
    more()
"""
    after = """def test_x(caplog):
    with log_records.set_level(logging.INFO):
        run()
        caplog.set_level(logging.DEBUG)
        more()
"""
    applied = _rewrite("VX205", before, after, _context("test_x"))

    assert _codes(applied) == ["VX205", "VX205"]


def test_a_set_level_with_nothing_after_it_is_refused() -> None:
    source = """def test_x(caplog):
    run()
    caplog.set_level(logging.INFO)
"""
    applied = _untouched("VX205", source, _context("test_x"))

    assert _codes(applied) == ["VX205"]


# --- VX209 pytest.raises ----------------------------------------------------------------------


def test_a_raises_block_becomes_velox_raises() -> None:
    before = """import pytest


def test_x():
    with pytest.raises(ValueError, match="nope") as info:
        boom()
    assert info.value
"""
    after = """import pytest


def test_x():
    with velox.raises(ValueError, match="nope") as info:
        boom()
    assert info.value
"""
    applied = _rewrite("VX209", before, after, _context("test_x"))

    assert _codes(applied) == ["VX209"]


def test_a_raises_called_with_a_func_becomes_velox_raises() -> None:
    """The callable form -- `pytest.raises(E, func, *args, **kwargs)` -- rewrites the same way
    the `with`-entered form does: only the name changes."""
    before = """import pytest


def test_x():
    pytest.raises(ValueError, boom, 1)
"""
    after = """import pytest


def test_x():
    velox.raises(ValueError, boom, 1)
"""
    applied = _rewrite("VX209", before, after, _context("test_x"))

    assert _codes(applied) == ["VX210"]


def test_a_raises_called_with_a_func_forwards_keyword_arguments_too() -> None:
    before = """import pytest


def test_x():
    pytest.raises(ValueError, boom, 1, kind="bad")
"""
    after = """import pytest


def test_x():
    velox.raises(ValueError, boom, 1, kind="bad")
"""
    applied = _rewrite("VX209", before, after, _context("test_x"))

    assert _codes(applied) == ["VX210"]


def test_a_raises_called_with_match_is_refused() -> None:
    """pytest's callable form forwards `match=` to `func` as one of its `**kwargs`; velox's
    callable form always intercepts `match` to match the exception instead. The two forms
    disagree on what the call means, so this is not a safe mechanical rewrite."""
    source = """import pytest


def test_x():
    pytest.raises(ValueError, boom, 1, match="bad")
"""
    applied = _untouched("VX209", source, _context("test_x"))

    assert _codes(applied) == ["VX210"]


def test_a_raises_called_over_a_cancellation_is_refused() -> None:
    source = """import asyncio

import pytest


def test_x():
    pytest.raises(asyncio.CancelledError, boom)
"""
    applied = _untouched("VX209", source, _context("test_x"))

    assert _codes(applied) == ["VX211"]


def test_a_raises_neither_entered_nor_called_is_refused() -> None:
    """A bare `pytest.raises(E)`, stashed for later rather than entered or called immediately,
    stays VX210: there is no `with` and no `func` for a rewrite to key off of."""
    source = """import pytest


def test_x():
    box = pytest.raises(ValueError)
"""
    applied = _untouched("VX209", source, _context("test_x"))

    assert _codes(applied) == ["VX210"]


def test_a_raises_over_a_cancellation_is_refused() -> None:
    source = """import asyncio

import pytest


async def test_x():
    with pytest.raises(asyncio.CancelledError):
        await boom()
"""
    applied = _untouched("VX209", source, _context("test_x"))

    assert _codes(applied) == ["VX211"]


def test_an_aliased_pytest_import_is_the_same_raises() -> None:
    before = """import pytest as pt


def test_x():
    with pt.raises(ValueError):
        boom()
"""
    after = """import pytest as pt


def test_x():
    with velox.raises(ValueError):
        boom()
"""
    _rewrite("VX209", before, after, _context("test_x"))


# --- VX212 pytest.approx ---------------------------------------------------------------------


def test_approx_over_a_scalar_becomes_velox_approx() -> None:
    before = """import pytest


def test_x():
    assert value == pytest.approx(0.3, rel=1e-6)
"""
    after = """import pytest


def test_x():
    assert value == velox.approx(0.3, rel=1e-6)
"""
    applied = _rewrite("VX212", before, after, _context("test_x"))

    assert _codes(applied) == ["VX212"]


def test_approxs_positional_tolerance_is_named() -> None:
    before = """import pytest


def test_x():
    assert value == pytest.approx(0.3, 1e-6)
"""
    after = """import pytest


def test_x():
    assert value == velox.approx(0.3, rel=1e-6)
"""
    _rewrite("VX212", before, after, _context("test_x"))


def test_approx_over_a_list_becomes_velox_approx() -> None:
    before = """import pytest


def test_x():
    assert values == pytest.approx([0.1, 0.2])
"""
    after = """import pytest


def test_x():
    assert values == velox.approx([0.1, 0.2])
"""
    applied = _rewrite("VX212", before, after, _context("test_x"))

    assert _codes(applied) == ["VX213"]


def test_approx_over_a_tuple_becomes_velox_approx() -> None:
    before = """import pytest


def test_x():
    assert values == pytest.approx((0.1, 0.2), rel=1e-6)
"""
    after = """import pytest


def test_x():
    assert values == velox.approx((0.1, 0.2), rel=1e-6)
"""
    applied = _rewrite("VX212", before, after, _context("test_x"))

    assert _codes(applied) == ["VX213"]


def test_approx_over_a_dict_becomes_velox_approx() -> None:
    before = """import pytest


def test_x():
    assert values == pytest.approx({"a": 0.1, "b": 0.2})
"""
    after = """import pytest


def test_x():
    assert values == velox.approx({"a": 0.1, "b": 0.2})
"""
    applied = _rewrite("VX212", before, after, _context("test_x"))

    assert _codes(applied) == ["VX213"]


def test_approx_over_a_list_comprehension_becomes_velox_approx() -> None:
    before = """import pytest


def test_x():
    assert values == pytest.approx([x for x in [0.1, 0.2]])
"""
    after = """import pytest


def test_x():
    assert values == velox.approx([x for x in [0.1, 0.2]])
"""
    applied = _rewrite("VX212", before, after, _context("test_x"))

    assert _codes(applied) == ["VX213"]


def test_approx_over_a_set_is_refused() -> None:
    source = """import pytest


def test_x():
    assert values == pytest.approx({0.1, 0.2})
"""
    applied = _untouched("VX212", source, _context("test_x"))

    assert _codes(applied) == ["VX221"]


def test_approx_over_a_generator_is_refused() -> None:
    source = """import pytest


def test_x():
    assert values == pytest.approx(x for x in [0.1, 0.2])
"""
    applied = _untouched("VX212", source, _context("test_x"))

    assert _codes(applied) == ["VX221"]


def test_approx_over_a_numpy_array_is_refused() -> None:
    source = """import numpy as np, pytest


def test_x():
    assert values == pytest.approx(np.array([0.1, 0.2]))
"""
    applied = _untouched("VX212", source, _context("test_x"))

    assert _codes(applied) == ["VX221"]


def test_approx_over_a_list_nested_in_a_list_is_refused() -> None:
    source = """import pytest


def test_x():
    assert values == pytest.approx([0.1, [0.2, 0.3]])
"""
    applied = _untouched("VX212", source, _context("test_x"))

    assert _codes(applied) == ["VX213"]


def test_approx_over_a_tuple_nested_in_a_dict_is_refused() -> None:
    source = """import pytest


def test_x():
    assert values == pytest.approx({"a": (0.1, 0.2)})
"""
    applied = _untouched("VX212", source, _context("test_x"))

    assert _codes(applied) == ["VX213"]


def test_approx_over_a_dict_nested_in_a_tuple_is_refused() -> None:
    source = """import pytest


def test_x():
    assert values == pytest.approx((0.1, {"a": 0.2}))
"""
    applied = _untouched("VX212", source, _context("test_x"))

    assert _codes(applied) == ["VX213"]


# --- across the whole set ---------------------------------------------------------------------


BLOCKED = """import pytest


@pytest.mark.parametrize("value", [1, 2])
@pytest.mark.skip(reason="later")
@pytest.mark.timeout(5)
def test_refused(value, capsys, caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(ValueError):
        assert capsys.readouterr().out == pytest.approx(0.1)
    assert caplog.records
"""


def test_a_blocked_function_is_left_verbatim_by_every_rule() -> None:
    context = _context("test_refused", blocked=["test_refused"])
    result = _all(BLOCKED, context)

    assert result.module.code == BLOCKED
    assert result.applied == ()


def test_a_blocked_method_is_left_verbatim_while_its_sibling_converts() -> None:
    before = """import pytest


class TestGroup:
    @pytest.mark.skip(reason="later")
    def test_refused(self):
        pass

    @pytest.mark.skip(reason="later")
    def test_converted(self):
        pass
"""
    after = """import pytest


class TestGroup:
    @pytest.mark.skip(reason="later")
    def test_refused(self):
        pass

    @velox.skip("later")
    def test_converted(self):
        pass
"""
    context = _context(
        "TestGroup.test_refused", "TestGroup.test_converted", blocked=["TestGroup.test_refused"]
    )
    result = _all(before, context)

    assert result.module.code == after


def test_a_body_rule_reads_a_fixture_and_a_helper_too() -> None:
    before = """import pytest


@pytest.fixture()
def engine(caplog):
    assert caplog.records == []
    with pytest.raises(ValueError):
        connect()
"""
    after = """import pytest


@pytest.fixture()
def engine(caplog):
    assert log_records.records == []
    with velox.raises(ValueError):
        connect()
"""
    result = _all(before, _context("test_x"))

    assert result.module.code == after


def test_a_function_the_dump_never_collected_keeps_its_marks() -> None:
    source = """import pytest


@pytest.mark.skip(reason="not a test")
def helper():
    pass
"""
    result = _all(source, _context("test_x"))

    assert result.module.code == source


WHOLE = '''"""A suite of one file."""

import logging

import pytest as pt

pytestmark = pt.mark.slow


@pt.mark.parametrize("value", [1, 2])
@pt.mark.skipif("sys.platform == 'win32'", reason="posix only")
@pt.mark.timeout(5)
@pt.mark.asyncio
async def test_x(value, capsys, caplog):
    """Its own docstring."""
    caplog.set_level(logging.INFO)
    with pt.raises(ValueError):  # loud
        assert capsys.readouterr().out == pt.approx(0.1)
    assert caplog.messages == []
'''


def test_the_whole_set_over_one_file_is_idempotent() -> None:
    context = _context("test_x", axis_ids={("test_x", "value"): ("1", "2")})
    once = _all(WHOLE, context)
    twice = _all(once.module.code, context)

    assert twice.module.code == once.module.code


def test_the_whole_set_writes_every_construct_in_one_file() -> None:
    context = _context("test_x", axis_ids={("test_x", "value"): ("1", "2")})
    result = _all(WHOLE, context)

    assert (
        result.module.code
        == '''"""A suite of one file."""

import logging

import pytest as pt


@velox.tag("slow")
@velox.parametrize("value", [1, 2], ids=("1", "2"))
@velox.skipif(lambda: sys.platform == 'win32', reason="posix only")
@velox.timeout(5)
async def test_x(value, capsys, caplog):
    """Its own docstring."""
    with log_records.set_level(logging.INFO):
        with velox.raises(ValueError):  # loud
            assert capture.out == velox.approx(0.1)
        assert log_records.messages == []
'''
    )


@pytest.mark.parametrize("code", sorted(BY_CODE))
def test_every_rule_leaves_a_file_it_has_nothing_to_do_with_alone(code: str) -> None:
    source = '''"""Nothing pytest here."""

import logging


def test_x():
    logging.info("hello")
    assert True
'''
    assert _untouched(code, source, _context("test_x")) == ()
