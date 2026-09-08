"""Tests for voci._warnings: filter-spec parsing, the shim's per-warning decision, and the
per-test/session collectors it records into.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import pytest

from voci import _warnings
from voci._assertions._vendor.rewrite import VociAssertRewriteWarning


@pytest.fixture
def installed() -> Iterator[None]:
    """The shim in place for one test with no session filters, undone whatever the test does."""
    _warnings.install()
    try:
        yield
    finally:
        _warnings.uninstall()


class Custom(UserWarning):
    """A `UserWarning` subclass, for checking that a filter reaches subclasses of its own
    category and no further."""


# Parsing.
# --------


def test_a_bare_action_is_the_whole_spec() -> None:
    parsed = _warnings.parse_filter("ignore")

    assert parsed.action == "ignore"
    assert parsed.message is None
    assert parsed.category is Warning
    assert parsed.module is None
    assert parsed.lineno == 0


def test_every_field_is_parsed() -> None:
    parsed = _warnings.parse_filter("error:.*legacy:DeprecationWarning:myapp[.]db:42")

    assert parsed.action == "error"
    assert parsed.message is not None and parsed.message.pattern == ".*legacy"
    assert parsed.category is DeprecationWarning
    assert parsed.module is not None and parsed.module.pattern == "myapp[.]db"
    assert parsed.lineno == 42


def test_an_empty_action_means_default() -> None:
    assert _warnings.parse_filter("::DeprecationWarning").action == "default"


def test_an_action_may_be_abbreviated() -> None:
    assert _warnings.parse_filter("e").action == "error"
    assert _warnings.parse_filter("ign").action == "ignore"


def test_a_dotted_category_is_imported() -> None:
    parsed = _warnings.parse_filter(
        "ignore::voci._assertions._vendor.rewrite.VociAssertRewriteWarning"
    )

    assert parsed.category is VociAssertRewriteWarning


@pytest.mark.parametrize(
    "spec",
    [
        "nope",
        "ignore::NotAWarning",
        "ignore::voci.nothing.AtAll",
        "ignore::int",
        "ignore:(unclosed:UserWarning",
        "ignore::UserWarning::nine",
        "ignore::UserWarning::-1",
        "ignore:a:b:c:d:e",
    ],
)
def test_a_malformed_spec_is_a_filter_error(spec: str) -> None:
    with pytest.raises(_warnings.FilterError):
        _warnings.parse_filter(spec)


def test_message_matching_is_case_insensitive_and_anchored_at_the_start() -> None:
    parsed = _warnings.parse_filter("ignore:OLD api")

    assert parsed.matches("old api is going away", UserWarning, "m", 1)
    assert not parsed.matches("the old api is going away", UserWarning, "m", 1)


def test_a_filter_matches_subclasses_of_its_category() -> None:
    parsed = _warnings.parse_filter("ignore::UserWarning")

    assert parsed.matches("x", Custom, "m", 1)
    assert not parsed.matches("x", DeprecationWarning, "m", 1)


# The shim's decision.
# --------------------


def test_a_warning_with_no_test_running_lands_on_the_session(installed: None) -> None:
    warnings.warn("session-level", UserWarning, stacklevel=1)

    (recorded,) = [w for w in _warnings.session_warnings() if w.message == "session-level"]
    assert recorded.category == "UserWarning"
    assert recorded.count == 1


def test_a_collected_warning_is_attributed_to_its_own_collector(installed: None) -> None:
    with _warnings.collecting() as collector:
        warnings.warn("mine", UserWarning, stacklevel=1)

    (recorded,) = collector.recorded()
    assert recorded.message == "mine"
    assert not [w for w in _warnings.session_warnings() if w.message == "mine"]


def test_repeats_of_one_warning_are_counted_rather_than_listed(installed: None) -> None:
    with _warnings.collecting() as collector:
        for _ in range(3):
            warnings.warn("again", UserWarning, stacklevel=1)

    (recorded,) = collector.recorded()
    assert recorded.count == 3


def test_a_non_always_action_records_a_warning_once(installed: None) -> None:
    with _warnings.collecting(_warnings.parse_filters(["once::UserWarning"])) as collector:
        for _ in range(3):
            warnings.warn("again", UserWarning, stacklevel=1)

    (recorded,) = collector.recorded()
    assert recorded.count == 1


def test_ignore_records_nothing(installed: None) -> None:
    with _warnings.collecting(_warnings.parse_filters(["ignore::UserWarning"])) as collector:
        warnings.warn("silenced", UserWarning, stacklevel=1)

    assert collector.recorded() == ()


def test_error_raises_the_warning_at_the_call_that_raised_it(installed: None) -> None:
    with (
        _warnings.collecting(_warnings.parse_filters(["error::UserWarning"])) as collector,
        pytest.raises(UserWarning, match="escalated"),
    ):
        warnings.warn("escalated", UserWarning, stacklevel=1)

    assert collector.recorded() == ()


def test_the_last_matching_filter_decides(installed: None) -> None:
    filters = _warnings.parse_filters(["error::UserWarning", "ignore::UserWarning"])
    with _warnings.collecting(filters) as collector:
        warnings.warn("not an error", UserWarning, stacklevel=1)

    assert collector.recorded() == ()


def test_a_test_filter_overrides_the_session_one() -> None:
    _warnings.install(_warnings.parse_filters(["ignore::UserWarning"]))
    try:
        with (
            _warnings.collecting(_warnings.parse_filters(["error::UserWarning"])),
            pytest.raises(UserWarning),
        ):
            warnings.warn("escalated after all", UserWarning, stacklevel=1)
    finally:
        _warnings.uninstall()


def test_a_module_filter_matches_the_raising_module(installed: None) -> None:
    # This test module is imported by pytest and so is in `sys.modules`, which is what lets the
    # shim resolve the warning's filename back to the dotted name a `module` field matches.
    filters = _warnings.parse_filters([f"ignore::UserWarning:{__name__}"])
    with _warnings.collecting(filters) as collector:
        warnings.warn("from this module", UserWarning, stacklevel=1)

    assert collector.recorded() == ()


def test_a_module_filter_naming_something_else_leaves_the_warning_alone(installed: None) -> None:
    filters = _warnings.parse_filters(["ignore::UserWarning:not[.]this[.]one"])
    with _warnings.collecting(filters) as collector:
        warnings.warn("survives", UserWarning, stacklevel=1)

    assert len(collector.recorded()) == 1


def test_only_so_many_distinct_warnings_are_kept() -> None:
    collector = _warnings._Collector((), limit=2)

    for i in range(5):
        collector.handle(UserWarning(f"number {i}"), UserWarning, "m.py", i)

    assert len(collector.recorded()) == 2


# Installation.
# -------------


def test_install_reports_whether_it_was_the_one_that_installed() -> None:
    assert _warnings.install() is True
    try:
        assert _warnings.install() is False
    finally:
        _warnings.uninstall()


def test_uninstall_restores_the_previous_showwarning() -> None:
    before = warnings.showwarning
    _warnings.install()
    assert warnings.showwarning is not before
    _warnings.uninstall()
    assert warnings.showwarning is before


def test_a_swallow_hook_claims_a_warning_before_any_filter(installed: None) -> None:
    claimed: list[str] = []

    def hook(message: Warning | str, category: type[Warning], filename: str, lineno: int) -> bool:
        claimed.append(str(message))
        return True

    _warnings.register_swallow(hook)
    try:
        with _warnings.collecting(_warnings.parse_filters(["error"])) as collector:
            warnings.warn("claimed", UserWarning, stacklevel=1)
    finally:
        _warnings.unregister_swallow(hook)

    assert claimed == ["claimed"]
    assert collector.recorded() == ()


def test_unregistering_a_hook_that_was_never_registered_is_silent() -> None:
    _warnings.unregister_swallow(lambda message, category, filename, lineno: False)
