"""Tests for velox_migrate.matrix: the support matrix's own invariants.

The matrix is data every other stage reads, and a wrong row is a wrong report, a wrong marker and
a wrong rewrite rule at once — so its shape is asserted rather than trusted.
"""

from __future__ import annotations

import pytest
from velox_migrate import matrix
from velox_migrate.matrix import Area, Disposition


def test_every_code_is_unique() -> None:
    codes = [construct.code for construct in matrix.CONSTRUCTS]

    assert len(codes) == len(set(codes))


def test_every_area_holds_the_codes_blocked_under_it() -> None:
    grouped = dict(matrix.by_area())

    assert sum(len(rows) for rows in grouped.values()) == len(matrix.CONSTRUCTS)
    assert grouped[Area.HAZARDS] and all(row.code[2] == "4" for row in grouped[Area.HAZARDS])


def test_a_marker_category_belongs_to_a_marker_row_only() -> None:
    for construct in matrix.CONSTRUCTS:
        assert (construct.marker is not None) == (construct.disposition is Disposition.MARKER)


def test_everything_that_does_not_convert_says_what_a_human_does_instead() -> None:
    for construct in matrix.CONSTRUCTS:
        if construct.disposition is not Disposition.MECHANICAL:
            assert construct.action, construct.code


def test_only_hazards_can_go_undetected() -> None:
    # A construct nothing can see is reported as a blind spot, and a blind spot that was meant to
    # be convertible would silently convert nothing.
    for construct in matrix.CONSTRUCTS:
        if not construct.detected:
            assert construct.disposition is Disposition.HAZARD


def test_mechanical_and_marker_rows_are_the_ones_conversion_produces_code_for() -> None:
    converts = {construct.code for construct in matrix.CONSTRUCTS if construct.converts}

    assert "VX001" in converts
    assert "VX214" not in converts


def test_a_code_the_matrix_does_not_define_is_refused_by_name() -> None:
    with pytest.raises(KeyError, match="VX999"):
        matrix.construct("VX999")


def test_marker_categories_are_the_spellings_marked_source_carries() -> None:
    assert "capture" in matrix.MARKER_CATEGORIES
    assert len(matrix.MARKER_CATEGORIES) == len(set(matrix.MARKER_CATEGORIES))
    assert all(category for category in matrix.MARKER_CATEGORIES)


def test_a_plugin_nobody_has_looked_at_has_no_path() -> None:
    recipe = matrix.plugin("pytest-something-nobody-wrote-a-recipe-for")

    assert recipe.construct.disposition is Disposition.UNSUPPORTED
    assert recipe.code == "VX323"


def test_a_known_plugin_carries_its_own_note() -> None:
    recipe = matrix.plugin("pytest-asyncio")

    assert recipe.construct.disposition is Disposition.MECHANICAL
    assert "async" in recipe.note


def test_every_table_cites_a_row_that_exists() -> None:
    cited = {
        *matrix.BUILTIN_FIXTURES.values(),
        *matrix.INI_SETTINGS.values(),
        *(recipe.code for recipe in matrix.PLUGINS.values()),
    }

    assert cited <= set(matrix.BY_CODE)


def test_every_plugin_mark_names_a_plugin_with_a_recipe() -> None:
    assert set(matrix.PLUGIN_MARKS.values()) <= set(matrix.PLUGINS)


def test_marks_pytest_answers_itself_are_not_filed_as_a_plugins() -> None:
    # A mark in both tables would be classified twice, once as a tag and once as a plugin's.
    assert not matrix.KNOWN_MARKS & set(matrix.PLUGIN_MARKS)
