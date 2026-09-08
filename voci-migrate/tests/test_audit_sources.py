"""Tests for voci_migrate.audit.sources: what reading a suite's own sources finds in it.

Almost every test scans one snippet and asserts the support-matrix codes it yields, because a code
is what a report groups by and what a rewrite rule keys off; the line and the enclosing function
are asserted where they are the point. Several snippets carry a construct that must not be
reported next to the one that must, so a rule that over-reaches fails the same test it passes.
`scan` itself is exercised over real files, for the part `scan_source` does not do.
"""

from __future__ import annotations

from pathlib import Path

from voci_migrate.audit import sources
from voci_migrate.audit.findings import Finding

PATH = "tests/test_suite.py"


def _codes(source: str) -> list[str]:
    return [finding.code for finding in sources.scan_source(source, path=PATH)]


def _only(source: str) -> Finding:
    found = sources.scan_source(source, path=PATH)
    assert len(found) == 1, [(f.code, f.message) for f in found]
    return found[0]


def _write(directory: Path, name: str, source: str) -> Path:
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


# --- wiring and request ------------------------------------------------------------------------


def test_a_literal_fixture_name_is_a_static_dependency() -> None:
    finding = _only("""
@pytest.fixture
def engine(request):
    return request.getfixturevalue("database")
""")

    assert finding.code == "VC011"
    assert "database" in finding.message
    # The name is what the conversion injects, so it travels as data rather than as prose.
    assert finding.detail == {"requested": "database"}


def test_a_computed_fixture_name_is_a_different_row_from_a_literal_one() -> None:
    source = """
@pytest.fixture
def engine(request, name):
    return request.getfixturevalue(name)
"""

    assert _codes(source) == ["VC012"]


def test_a_finalizer_at_the_top_of_a_body_is_unconditional() -> None:
    source = """
@pytest.fixture
def engine(request):
    request.addfinalizer(close)
"""

    assert _codes(source) == ["VC013"]


def test_a_finalizer_inside_a_block_names_the_block_it_is_under() -> None:
    finding = _only("""
@pytest.fixture
def engine(request):
    if wants_teardown:
        request.addfinalizer(close)
""")

    assert finding.code == "VC014"
    assert "`if`" in finding.message


def test_a_finalizer_in_a_with_body_is_unconditional_and_one_in_a_try_body_is_not() -> None:
    # Entering a `with` can raise, but then nothing after it runs either, so a registration in one
    # happens exactly when the fixture succeeds. A `try` body is different: its own handler can
    # swallow the failure that stopped it halfway and let the rest of the fixture run without the
    # finalizer that line would have registered.
    source = """
@pytest.fixture
def engine(request):
    with open("f") as handle:
        request.addfinalizer(handle.close)

@pytest.fixture
def session(request):
    try:
        request.addfinalizer(close)
    except OSError:
        request.addfinalizer(other)
"""

    assert _codes(source) == ["VC013", "VC014", "VC014"]


def test_a_finalizer_inside_a_match_case_is_conditional() -> None:
    finding = _only("""
@pytest.fixture
def engine(request, mode):
    match mode:
        case "eager":
            request.addfinalizer(close)
""")

    assert finding.code == "VC014"
    assert "`case`" in finding.message


def test_a_finalizer_in_a_nested_def_is_judged_by_that_def_s_own_body() -> None:
    # What matters is the branching in the function that registers the finalizer, not how deep in
    # the file that function itself sits.
    source = """
@pytest.fixture
def engine(request):
    if slow:
        def register():
            request.addfinalizer(close)
"""

    assert _codes(source) == ["VC013"]


def test_the_request_attributes_with_no_counterpart_are_reported() -> None:
    source = """
@pytest.fixture
def engine(request):
    print(request.node)
    print(request.cls)
    print(request.fixturenames)
"""

    assert _codes(source) == ["VC015"] * 3


def test_an_attribute_of_request_with_no_row_of_its_own_is_still_reported() -> None:
    # Whether `request` survives the rewrite is what decides if the parameter can go, so an
    # attribute nothing has a translation for has to be seen rather than passed over.
    finding = _only("""
@pytest.fixture
def engine(request):
    request.applymarker(slow)
""")

    assert finding.code == "VC015"
    assert "request.applymarker" in finding.message


def test_the_three_request_shapes_a_rewrite_answers_are_not_reported_as_survivals() -> None:
    source = """
@pytest.fixture
def engine(request):
    request.addfinalizer(close)
    return request.getfixturevalue("database"), request.param
"""

    assert _codes(source) == ["VC013", "VC011"]


def test_reading_a_command_line_flag_is_reported_as_the_flag_and_nothing_else() -> None:
    # `request.config` is a row of its own, so the intermediate attribute must not double-report.
    source = """
@pytest.fixture
def engine(request):
    return request.config.getoption("--slow")
"""

    assert _codes(source) == ["VC016"]


def test_request_handed_on_rather_than_read_is_reported_as_held() -> None:
    source = """
@pytest.fixture
def engine(request):
    configure(request)
    return {"request": request}
"""

    assert _codes(source) == ["VC017", "VC017"]


def test_a_local_variable_named_request_is_not_the_fixture() -> None:
    source = """
def test_a():
    request = build_request()
    send(request)
    return request.node
"""

    assert _codes(source) == []


def test_a_request_parameter_of_a_function_pytest_never_calls_is_not_the_fixture() -> None:
    # An HTTP suite writes mock applications and auth flows that take a request of their own, and
    # nothing but the name it is spelled with connects those to pytest's.
    source = """
class App:
    def __call__(self, request):
        return Response(200, headers=request.headers)

def handler(request):
    send(request)
"""

    assert _codes(source) == []


def test_a_builtin_handed_to_a_helper_is_still_read_there() -> None:
    # `request` is the only one of these names a suite also spells for an object of its own, so
    # the rest are pytest's wherever they are declared: the helper is where the hazard is written.
    source = """
def _patch_env(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")

def test_a(monkeypatch):
    _patch_env(monkeypatch)
"""

    assert _codes(source) == ["VC401"]


def test_request_forwarded_into_a_helper_is_still_read_there() -> None:
    # Unlike `monkeypatch` above, the forwarding call site itself has to hold pytest's own
    # `request` before the helper's identically named parameter is trusted -- which is what keeps
    # `handler`'s own `request` two tests up unrecognized, since nothing there ever calls it.
    source = """
def _register_cleanup(request):
    request.addfinalizer(close)

def test_a(request):
    _register_cleanup(request)
"""

    assert _codes(source) == ["VC013", "VC017"]


def test_a_chain_of_forwarding_helpers_is_read_all_the_way_in() -> None:
    # `test_a` hands `request` to `_outer`, which hands it on to `_inner` -- two hops, not the one
    # a single extra pass would catch.
    source = """
def _inner(request):
    request.addfinalizer(close)

def _outer(request):
    _inner(request)

def test_a(request):
    _outer(request)
"""

    assert _codes(source) == ["VC013", "VC017", "VC017"]


def test_two_helpers_sharing_a_name_in_different_scopes_are_not_conflated() -> None:
    # The module-level `helper` is genuinely forwarded pytest's `request`; the one nested inside
    # `test_b` shares the name but takes a request of its own, and forwarding one must not mark
    # the other.
    source = """
def helper(request):
    return request.node

def test_a(request):
    helper(request)

def test_b():
    def helper(request):
        return request.node
    helper(build_request())
"""

    assert _codes(source) == ["VC015", "VC017"]


def test_a_test_and_a_test_method_take_the_request_pytest_injects() -> None:
    source = """
def test_engine(request):
    return request.node

class TestEngine:
    def test_session(self, request):
        return request.session
"""

    assert _codes(source) == ["VC015", "VC015"]


def test_a_nested_def_of_its_own_request_shadows_the_fixture_above_it() -> None:
    # The parameter is filled in by whatever calls the inner function, which is the fixture body
    # rather than pytest, so the fixture's own `request` is not what is being read.
    source = """
@pytest.fixture
def engine(request):
    def callback(request):
        return request.node
    return callback
"""

    assert _codes(source) == []


def test_reading_request_attribute_by_attribute_is_not_holding_it() -> None:
    source = """
@pytest.fixture
def engine(request):
    return request.node.name
"""

    assert _codes(source) == ["VC015"]


def test_the_setup_protocol_is_reported_wherever_it_is_defined() -> None:
    found = sources.scan_source(
        """
def setup_function(function):
    pass
class TestGroup:
    def teardown_method(self):
        pass
""",
        path=PATH,
    )

    assert [(f.code, f.site.function) for f in found] == [
        ("VC019", "setup_function"),
        ("VC019", "TestGroup.teardown_method"),
    ]


def test_a_class_fixture_reading_a_class_attribute_survives_being_lifted() -> None:
    # `MySchema` is written in the class body, so the class name still reaches it once the factory
    # is a module-level object. Nothing to refuse.
    source = """
import pytest
class TestGroup:
    class MySchema:
        pass
    @pytest.fixture
    def schema(self):
        return self.MySchema()
"""

    assert _codes(source) == []


def test_a_class_fixture_reading_anything_else_off_self_is_refused() -> None:
    found = sources.scan_source(
        """
import pytest
class TestGroup:
    @pytest.fixture
    def handed_on(self):
        return helper(self)
    @pytest.fixture
    def instance_only(self):
        return self.built_by_another_fixture
    @pytest.fixture
    def written_onto(self):
        self.stamp = 1
        return self.stamp
""",
        path=PATH,
    )

    assert [(f.code, f.site.function) for f in found] == [
        ("VC033", "TestGroup.handed_on"),
        ("VC033", "TestGroup.instance_only"),
        ("VC033", "TestGroup.written_onto"),
    ]


def test_an_annotation_with_no_value_binds_nothing_on_the_class() -> None:
    # `client: Client` says what an instance will carry; reading it off the class raises, so the
    # factory cannot be lifted.
    source = """
import pytest
class TestGroup:
    client: object
    @pytest.fixture
    def wired(self):
        return self.client
"""

    assert _codes(source) == ["VC033"]


def test_a_self_a_nested_def_declares_is_not_the_factorys() -> None:
    # The inner `self` belongs to `label`, and lifting the factory out leaves it exactly where it
    # was — reporting it would refuse a fixture that converts fine.
    source = """
import pytest
class TestGroup:
    @pytest.fixture
    def built(self):
        class Built:
            def label(self):
                return self.marker
        return Built()
"""

    assert _codes(source) == []


def test_a_plain_method_is_not_read_for_self_at_all() -> None:
    # Only a fixture is lifted out of its class; a test method keeps the receiver voci builds it.
    source = """
class TestGroup:
    def test_a(self):
        assert self.__class__
"""

    assert _codes(source) == []


def test_a_familiar_name_outside_the_protocol_is_an_ordinary_function() -> None:
    # pytest calls `setup_method` on a class and `setup_function` on a module. A fixture called
    # `setup`, or a helper nested in a test, is neither, and reporting it would refuse tests that
    # convert fine.
    source = """
import pytest
@pytest.fixture
def setup():
    return object()
def teardown_method(self):
    pass
def test_a():
    def setup_method():
        pass
    setup_method()
"""

    assert _codes(source) == []


def test_a_testcase_subclass_is_recognized_through_an_alias_or_a_direct_import() -> None:
    source = """
import unittest as ut
from unittest import TestCase
class TestLegacy(ut.TestCase):
    pass
class TestOlder(TestCase):
    pass
"""

    assert _codes(source) == ["VC020", "VC020"]


def test_each_module_level_hook_is_reported_as_its_own_row() -> None:
    source = """
def pytest_collection_modifyitems(items):
    pass
def pytest_addoption(parser):
    pass
def pytest_generate_tests(metafunc):
    pass
"""

    assert _codes(source) == ["VC022", "VC023", "VC024"]


def test_a_method_named_like_a_hook_is_not_a_hook() -> None:
    # pytest calls hooks written at module level, and a suite's own class is free to borrow a name.
    source = """
class Helper:
    def pytest_configure(self, config):
        pass
"""

    assert _codes(source) == []


def test_a_plugin_declaration_names_what_it_pulls_in() -> None:
    finding = _only("""
pytest_plugins = ["tests.fixtures.db"]
""")

    assert finding.code == "VC025"
    assert "tests.fixtures.db" in finding.message


# --- known_tests from a dump --------------------------------------------------------------------


def test_a_customized_python_functions_pattern_is_recognized_from_a_dump() -> None:
    # `python_functions = ["*_check"]` collects `thing_check` as a test, which starts with none of
    # the prefixes a name-only guess could use; `known_tests` is the dump's own word on it, read
    # instead of the guess.
    source = """
def thing_check(request):
    return request.node
"""

    found = sources.scan_source(source, path=PATH, known_tests=frozenset({"thing_check"}))

    assert [finding.code for finding in found] == ["VC015"]


def test_a_name_starting_with_test_but_absent_from_the_dump_is_not_one() -> None:
    # Once a dump is in hand, starting with "test" is not enough on its own either: the dump's
    # collected names replace the guess rather than widen it.
    source = """
def test_helper(request):
    return request.node
"""

    found = sources.scan_source(source, path=PATH, known_tests=frozenset())

    assert found == ()


def test_a_test_method_nested_two_classes_deep_still_matches_the_dump() -> None:
    # The dump's own `item.cls` is `TestOuter.TestInner`, pytest's `__qualname__` for the class --
    # not just the immediate one -- so `known_tests` is keyed the same way here.
    source = """
class TestOuter:
    class TestInner:
        def test_a(self, request):
            return request.node
"""

    found = sources.scan_source(
        source, path=PATH, known_tests=frozenset({"TestOuter.TestInner.test_a"})
    )

    assert [finding.code for finding in found] == ["VC015"]


# --- marks -------------------------------------------------------------------------------------


def test_a_mark_on_one_parametrized_case_is_reported() -> None:
    source = """
import pytest
@pytest.mark.parametrize("n", [pytest.param(1, marks=pytest.mark.xfail)])
def test_a(n):
    pass
"""

    assert _codes(source) == ["VC102"]


def test_a_string_skipif_condition_is_reported_and_an_expression_is_left_to_the_dump() -> None:
    source = """
import pytest, sys
@pytest.mark.skipif("sys.platform == 'win32'", reason="posix")
@pytest.mark.skipif(condition="sys.version_info < (3, 13)", reason="new")
@pytest.mark.skipif(sys.platform == "win32", reason="posix")
def test_a():
    pass
"""

    assert _codes(source) == ["VC103", "VC103"]


def test_a_conditional_xfail_is_reported_and_an_unconditional_one_is_not() -> None:
    source = """
import pytest, sys
@pytest.mark.xfail(sys.platform == "win32", reason="known")
def test_a():
    pass
@pytest.mark.xfail(reason="known", strict=True)
def test_b():
    pass
"""

    assert _codes(source) == ["VC105"]


def test_an_xfail_that_does_not_run_the_test_is_its_own_row() -> None:
    source = """
import pytest
@pytest.mark.xfail(run=False, reason="hangs")
def test_a():
    pass
"""

    assert _codes(source) == ["VC106"]


def test_a_per_test_warning_filter_is_reported() -> None:
    source = """
import pytest
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a():
    pass
"""

    assert _codes(source) == ["VC108"]


# --- bodies and builtin fixtures ---------------------------------------------------------------


def test_repeated_output_reads_are_counted_and_sited_at_the_second_one() -> None:
    finding = _only("""
def test_a(capsys):
    capsys.readouterr()
    run()
    capsys.readouterr()
    capsys.readouterr()
""")

    assert (finding.code, finding.site.line, finding.site.function) == ("VC202", 5, "test_a")
    assert "3 times" in finding.message


def test_one_output_read_per_test_is_not_a_finding() -> None:
    source = """
def test_a(capsys):
    capsys.readouterr()
def test_b(capsys):
    capsys.readouterr()
"""

    assert _codes(source) == []


def test_setting_a_log_level_is_reported_apart_from_reading_the_records() -> None:
    source = """
import logging
def test_a(caplog):
    caplog.set_level(logging.DEBUG)
    assert caplog.records
"""

    assert _codes(source) == ["VC205"]


def test_the_log_attributes_with_no_counterpart_are_reported() -> None:
    source = """
def test_a(caplog):
    assert caplog.get_records("call")
    caplog.handler.flush()
"""

    assert _codes(source) == ["VC222"] * 2


def test_the_log_attributes_that_convert_are_not_reported() -> None:
    source = """
def test_a(caplog):
    assert caplog.text
    assert caplog.record_tuples
    caplog.clear()
"""

    assert _codes(source) == []


def test_a_stashed_raises_is_reported_and_the_callable_and_context_manager_forms_are_not() -> None:
    source = """
import pytest
def test_a():
    box = pytest.raises(ValueError)
def test_b():
    pytest.raises(ValueError, boom, 1)
def test_c():
    with pytest.raises(ValueError, match="boom"):
        boom()
"""

    assert _codes(source) == ["VC210"]


def test_a_callable_raises_with_match_is_reported() -> None:
    """pytest's callable form forwards `match=` to the callable rather than matching against the
    exception, unlike `voci.raises`'s callable form, which always intercepts it -- so this shape
    is flagged even though the plain callable form above is not."""
    source = """
import pytest
def test_a():
    pytest.raises(ValueError, boom, 1, match="boom")
"""

    assert _codes(source) == ["VC210"]


def test_catching_a_cancellation_is_recognized_through_an_import_of_the_exception() -> None:
    source = """
import pytest
from asyncio import CancelledError
async def test_a():
    with pytest.raises(CancelledError):
        await task
"""

    assert _codes(source) == ["VC211"]


def test_approx_over_a_set_a_generator_or_an_array_is_reported() -> None:
    source = """
import numpy as np, pytest
def test_a():
    assert a == pytest.approx({1.0, 2.0})
    assert b == pytest.approx(x for x in [1.0])
    assert c == pytest.approx(np.array([1.0]))
"""

    assert _codes(source) == ["VC221"] * 3


def test_approx_over_a_list_tuple_or_dict_is_not_reported() -> None:
    source = """
import pytest
def test_a():
    assert a == pytest.approx([1.0, 2.0])
    assert b == pytest.approx((1.0, 2.0))
    assert c == pytest.approx({"x": 1.0})
    assert d == pytest.approx([x for x in [1.0]])
    assert e == pytest.approx({x: x for x in [1.0]})
    assert f == pytest.approx(1.0, rel=1e-6)
"""

    assert _codes(source) == []


def test_approx_over_a_nested_container_is_reported() -> None:
    source = """
import pytest
def test_a():
    assert a == pytest.approx([0.1, [0.2, 0.3]])
    assert b == pytest.approx({"x": (0.1, 0.2)})
    assert c == pytest.approx((0.1, {"x": 0.2}))
"""

    assert _codes(source) == ["VC213"] * 3


def test_a_convertible_imperative_skip_or_fail_is_not_reported() -> None:
    """`pytest.skip("...")`/`pytest.fail("...")`, called with just a reason, become
    `raise voci.Skipped(...)`/`Failed(...)` -- there is nothing here for the audit to flag,
    unlike the decorator mark, which is a different construct with its own row entirely."""
    source = """
import pytest
@pytest.mark.skip(reason="flaky")
def test_a():
    if not available:
        pytest.skip("no backend")
    try:
        connect()
    except OSError:
        pytest.fail("unreachable")
"""

    assert _codes(source) == []


def test_an_unconvertible_imperative_skip_or_fail_is_reported() -> None:
    """`allow_module_level=`/`pytrace=` have no voci equivalent -- the rewrite rule leaves
    these as they are, so the audit still flags them."""
    source = """
import pytest
def test_a():
    if not available:
        pytest.skip("no backend", allow_module_level=True)
    try:
        connect()
    except OSError:
        pytest.fail("unreachable", pytrace=False)
"""

    assert _codes(source) == ["VC214", "VC214"]


def test_an_imperative_xfail_is_reported_as_having_no_runtime_target() -> None:
    """Unlike `pytest.skip()`/`pytest.fail()`, `pytest.xfail()` stays refused: an expectation
    `@voci.xfail(...)` decides once, at collection, has no runtime counterpart to raise."""
    source = """
import pytest
def test_a():
    if not available:
        pytest.xfail("no backend")
"""

    assert _codes(source) == ["VC223"]


def test_an_import_time_skip_is_reported() -> None:
    source = """
import pytest
lxml = pytest.importorskip("lxml")
"""

    assert _codes(source) == ["VC215"]


def test_recording_warnings_is_reported_in_both_spellings() -> None:
    source = """
import pytest
def test_a():
    with pytest.warns(UserWarning):
        warn()
    with pytest.deprecated_call():
        old()
"""

    assert _codes(source) == ["VC216", "VC216"]


def test_every_patch_decorator_form_is_reported_once_and_not_as_a_context_manager() -> None:
    source = """
import unittest.mock as m
from unittest.mock import patch
@m.patch.dict("os.environ", {"A": "b"})
@patch.object(Thing, "method")
@patch("app.client")
def test_a(client, method):
    pass
"""

    assert _codes(source) == ["VC217"] * 3


def test_a_patch_entered_in_the_body_is_reported_however_it_is_entered() -> None:
    source = """
from unittest import mock
def test_a():
    with mock.patch("app.client"):
        pass
    patcher = mock.patch("app.other")
    patcher.start()
    mock.patch("app.third").start()
"""

    assert _codes(source) == ["VC218"] * 3


def test_every_call_on_the_mocker_fixture_is_reported() -> None:
    source = """
def test_a(mocker):
    mocker.patch("app.client")
    mocker.spy(app, "send")
"""

    assert _codes(source) == ["VC219", "VC219"]


# --- hazards -----------------------------------------------------------------------------------


def test_every_monkeypatch_call_is_a_hazard_and_chdir_says_it_needs_isolation() -> None:
    found = sources.scan_source(
        """
def test_a(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setattr(app, "client", stub)
    monkeypatch.syspath_prepend("vendor")
    monkeypatch.chdir("/srv")
""",
        path=PATH,
    )

    assert [f.code for f in found] == ["VC401"] * 4
    assert "environment variable" in found[0].message
    assert "isolation" in found[3].message


def test_a_monkeypatch_parameter_is_told_from_a_local_of_the_same_name() -> None:
    source = """
def test_a():
    monkeypatch = Recorder()
    monkeypatch.setenv("TZ", "UTC")
"""

    assert _codes(source) == []


def test_every_environment_write_is_reported_and_a_read_is_not() -> None:
    source = """
import os
def test_a():
    os.environ["TZ"] = "UTC"
    os.environ.update(extra)
    os.environ.pop("TZ", None)
    del os.environ["HOME"]
    os.putenv("TZ", "UTC")
    assert os.environ.get("TZ")
"""

    assert _codes(source) == ["VC402"] * 5


def test_warning_filter_mutation_is_reported_in_every_spelling() -> None:
    source = """
import warnings
def test_a():
    warnings.simplefilter("error")
    warnings.filterwarnings("ignore")
    warnings.resetwarnings()
    with warnings.catch_warnings():
        pass
"""

    assert _codes(source) == ["VC403"] * 4


def test_logging_level_and_handler_mutation_is_reported() -> None:
    source = """
import logging
def test_a():
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger("app").setLevel(logging.DEBUG)
    logging.getLogger("app").addHandler(handler)
    logging.root.handlers = []
"""

    assert _codes(source) == ["VC404"] * 4


def test_module_surgery_is_reported_however_it_is_spelled() -> None:
    source = """
import importlib, sys
def test_a():
    sys.modules["app"] = stub
    sys.modules.pop("app", None)
    del sys.modules["app.db"]
    importlib.reload(app)
"""

    assert _codes(source) == ["VC405"] * 4


def test_changing_the_working_directory_is_reported() -> None:
    source = """
import contextlib, os
def test_a():
    os.chdir("/srv")
    with contextlib.chdir("/srv"):
        pass
"""

    assert _codes(source) == ["VC406", "VC406"]


def test_interpreter_wide_settings_are_reported() -> None:
    source = """
import decimal, locale, sys
def test_a():
    locale.setlocale(locale.LC_ALL, "C")
    decimal.setcontext(ctx)
    sys.setrecursionlimit(5000)
"""

    assert _codes(source) == ["VC407"] * 3


def test_a_patched_clock_is_reported_under_either_library() -> None:
    source = """
import time_machine
from freezegun import freeze_time
def test_a():
    with freeze_time("2026-01-01"):
        pass
    with time_machine.travel("2026-01-01"):
        pass
"""

    assert _codes(source) == ["VC408", "VC408"]


def test_driving_an_event_loop_is_reported() -> None:
    source = """
import asyncio
def test_a():
    asyncio.run(main())
    loop = asyncio.new_event_loop()
    loop.run_until_complete(main())
"""

    assert _codes(source) == ["VC409"] * 3


def test_a_blocking_call_in_an_async_body_is_reported() -> None:
    finding = _only("""
import time
async def test_a():
    time.sleep(0.1)
""")

    assert finding.code == "VC410"
    assert "time.sleep" in finding.message


def test_the_same_blocking_call_in_a_sync_body_is_not_a_finding() -> None:
    # A sync test runs on an executor thread, where a blocking call holds nothing but its own slot.
    source = """
import requests, time
def test_a():
    time.sleep(0.1)
    requests.get("https://example.test")
"""

    assert _codes(source) == []


def test_a_blocking_client_call_in_an_async_body_is_reported() -> None:
    source = """
import requests
async def test_a():
    requests.get("https://example.test")
"""

    assert _codes(source) == ["VC410"]


def test_state_that_outlives_a_test_is_reported_and_a_local_container_is_not() -> None:
    source = """
CACHE = {}
CALLS = 0
def test_a():
    global CALLS
    CACHE["key"] = 1
    local = {}
    local["key"] = 1
"""

    assert _codes(source) == ["VC411", "VC411"]


def test_a_module_name_a_test_rebinds_for_itself_is_that_tests_own() -> None:
    # This finding is `serialized`, so a false positive here inflates the headline share of the
    # suite that has to run alone.
    source = """
CACHE = {}
def test_a():
    CACHE = {}
    CACHE["key"] = 1
def test_b(CACHE):
    CACHE["key"] = 1
"""

    assert _codes(source) == []


def test_seeded_randomness_and_sequence_counters_are_reported() -> None:
    source = """
import numpy as np, random
from faker import Faker
def test_a():
    random.seed(0)
    np.random.seed(0)
    Faker.seed(0)
    UserFactory.reset_sequence(1)
"""

    assert _codes(source) == ["VC412"] * 4


def test_a_fixed_resource_is_reported_in_each_shape_it_is_recognized_by() -> None:
    source = """
def test_a(free_port):
    connect(host="db", Port=5432)
    connect(port=free_port)
    fetch("http://localhost:8000/health")
    open("/tmp/fixture.db")
"""

    assert _codes(source) == ["VC413"] * 3


# --- resolution, deduplication and whole files -------------------------------------------------


def test_an_aliased_import_resolves_to_the_construct_it_names() -> None:
    source = """
import pytest as pt
from pytest import raises
def test_a():
    box = raises(ValueError)
    pt.importorskip("lxml")
"""

    assert _codes(source) == ["VC210", "VC215"]


def test_one_finding_survives_per_code_line_and_function() -> None:
    source = """
@pytest.fixture
def engine(request):
    return (request.node, request.session)
"""

    assert _codes(source) == ["VC015"]


def test_a_finding_carries_the_path_it_was_given_and_a_one_based_line() -> None:
    finding = _only("""
import os
def test_a():
    os.chdir("/srv")
""")

    assert (finding.site.file, finding.site.line, finding.site.function) == (PATH, 4, "test_a")


def test_scanning_files_sites_them_relative_to_the_root(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    path = _write(
        tmp_path / "tests", "test_env.py", "import os\ndef test_a():\n    os.chdir('/')\n"
    )

    scan = sources.scan([path], root=tmp_path)

    assert scan.files == 1
    assert [(f.code, f.site.file) for f in scan.findings] == [("VC406", "tests/test_env.py")]


def test_scanning_skips_a_path_that_is_not_there(tmp_path: Path) -> None:
    scan = sources.scan([tmp_path / "gone.py"], root=tmp_path)

    assert (scan.files, scan.findings, scan.unparsed) == (0, (), ())


def test_a_file_that_will_not_parse_costs_its_own_findings_and_no_others(tmp_path: Path) -> None:
    broken = _write(tmp_path, "conftest.py", "def broken(:\n")
    fine = _write(tmp_path, "test_ok.py", "import os\ndef test_a():\n    os.chdir('/srv')\n")

    scan = sources.scan([broken, fine], root=tmp_path)

    assert (scan.unparsed, scan.files) == (("conftest.py",), 1)
    assert [f.code for f in scan.findings] == ["VC406"]


def test_scanning_orders_findings_worst_first(tmp_path: Path) -> None:
    # A report leads with what blocks conversion, so a hazard must not sort above an unsupported
    # construct that happens to sit earlier in the file.
    path = _write(
        tmp_path,
        "test_both.py",
        "import os, pytest\ndef test_a():\n    os.chdir('/srv')\n"
        "    with pytest.warns(UserWarning):\n        warn()\n",
    )

    scan = sources.scan([path], root=tmp_path)

    assert [f.code for f in scan.findings] == ["VC216", "VC406"]


def test_scanning_reads_each_path_once(tmp_path: Path) -> None:
    path = _write(tmp_path, "test_dup.py", "import os\ndef test_a():\n    os.chdir('/srv')\n")

    scan = sources.scan([path] * 2, root=tmp_path)

    assert scan.files == 1
    assert len(scan.findings) == 1


def test_scanning_threads_known_tests_through_to_each_path(tmp_path: Path) -> None:
    # `known_tests` is keyed the same way `scan`'s own paths are sited: relative to `root`.
    path = _write(tmp_path, "test_dsl.py", "def thing_check(request):\n    return request.node\n")

    known_tests = {"test_dsl.py": frozenset({"thing_check"})}
    scan = sources.scan([path], root=tmp_path, known_tests=known_tests)

    assert [f.code for f in scan.findings] == ["VC015"]
