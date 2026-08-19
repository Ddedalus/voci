"""Tests for velox_migrate.audit.sources: what reading a suite's own sources finds in it.

Almost every test scans one snippet and asserts the support-matrix codes it yields, because a code
is what a report groups by and what a rewrite rule keys off; the line and the enclosing function
are asserted where they are the point. Several snippets carry a construct that must not be
reported next to the one that must, so a rule that over-reaches fails the same test it passes.
`scan` itself is exercised over real files, for the part `scan_source` does not do.
"""

from __future__ import annotations

from pathlib import Path

from velox_migrate.audit import sources
from velox_migrate.audit.findings import Finding

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
def engine(request):
    return request.getfixturevalue("database")
""")

    assert finding.code == "VX011"
    assert "database" in finding.message


def test_a_computed_fixture_name_is_a_different_row_from_a_literal_one() -> None:
    source = """
def engine(request, name):
    return request.getfixturevalue(name)
"""

    assert _codes(source) == ["VX012"]


def test_a_finalizer_at_the_top_of_a_body_is_unconditional() -> None:
    source = """
def engine(request):
    request.addfinalizer(close)
"""

    assert _codes(source) == ["VX013"]


def test_a_finalizer_inside_a_block_names_the_block_it_is_under() -> None:
    finding = _only("""
def engine(request):
    if wants_teardown:
        request.addfinalizer(close)
""")

    assert finding.code == "VX014"
    assert "`if`" in finding.message


def test_a_finalizer_in_a_body_that_always_runs_is_unconditional() -> None:
    # A `with` body and a `try` body both run, so a finalizer registered in one is registered
    # every time, which is what `yield` teardown does.
    source = """
def engine(request):
    with open("f") as handle:
        request.addfinalizer(handle.close)

def session(request):
    try:
        request.addfinalizer(close)
    except OSError:
        request.addfinalizer(other)
"""

    assert _codes(source) == ["VX013", "VX013", "VX014"]


def test_a_finalizer_inside_a_match_case_is_conditional() -> None:
    finding = _only("""
def engine(request, mode):
    match mode:
        case "eager":
            request.addfinalizer(close)
""")

    assert finding.code == "VX014"
    assert "`case`" in finding.message


def test_a_finalizer_in_a_nested_def_is_judged_by_that_def_s_own_body() -> None:
    # What matters is the branching in the function that registers the finalizer, not how deep in
    # the file that function itself sits.
    source = """
def engine(request):
    if slow:
        def register():
            request.addfinalizer(close)
"""

    assert _codes(source) == ["VX013"]


def test_the_request_attributes_with_no_counterpart_are_reported() -> None:
    source = """
def engine(request):
    print(request.node)
    print(request.cls)
    print(request.fixturenames)
"""

    assert _codes(source) == ["VX015"] * 3


def test_reading_a_command_line_flag_is_reported_as_the_flag_and_nothing_else() -> None:
    # `request.config` is a row of its own, so the intermediate attribute must not double-report.
    source = """
def engine(request):
    return request.config.getoption("--slow")
"""

    assert _codes(source) == ["VX016"]


def test_request_handed_on_rather_than_read_is_reported_as_held() -> None:
    source = """
def engine(request):
    configure(request)
    return {"request": request}
"""

    assert _codes(source) == ["VX017", "VX017"]


def test_a_local_variable_named_request_is_not_the_fixture() -> None:
    source = """
def test_a():
    request = build_request()
    send(request)
    return request.node
"""

    assert _codes(source) == []


def test_reading_request_attribute_by_attribute_is_not_holding_it() -> None:
    source = """
def engine(request):
    return request.node.name
"""

    assert _codes(source) == ["VX015"]


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
        ("VX019", "setup_function"),
        ("VX019", "TestGroup.teardown_method"),
    ]


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

    assert _codes(source) == ["VX020", "VX020"]


def test_each_module_level_hook_is_reported_as_its_own_row() -> None:
    source = """
def pytest_collection_modifyitems(items):
    pass
def pytest_addoption(parser):
    pass
def pytest_generate_tests(metafunc):
    pass
"""

    assert _codes(source) == ["VX022", "VX023", "VX024"]


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

    assert finding.code == "VX025"
    assert "tests.fixtures.db" in finding.message


# --- marks -------------------------------------------------------------------------------------


def test_a_mark_on_one_parametrized_case_is_reported() -> None:
    source = """
import pytest
@pytest.mark.parametrize("n", [pytest.param(1, marks=pytest.mark.xfail)])
def test_a(n):
    pass
"""

    assert _codes(source) == ["VX102"]


def test_a_string_skipif_condition_is_reported_and_an_expression_is_left_to_the_dump() -> None:
    source = """
import pytest, sys
@pytest.mark.skipif("sys.platform == 'win32'", reason="posix")
@pytest.mark.skipif(condition="sys.version_info < (3, 13)", reason="new")
@pytest.mark.skipif(sys.platform == "win32", reason="posix")
def test_a():
    pass
"""

    assert _codes(source) == ["VX103", "VX103"]


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

    assert _codes(source) == ["VX105"]


def test_an_xfail_that_does_not_run_the_test_is_its_own_row() -> None:
    source = """
import pytest
@pytest.mark.xfail(run=False, reason="hangs")
def test_a():
    pass
"""

    assert _codes(source) == ["VX106"]


def test_a_per_test_warning_filter_is_reported() -> None:
    source = """
import pytest
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a():
    pass
"""

    assert _codes(source) == ["VX108"]


# --- bodies and builtin fixtures ---------------------------------------------------------------


def test_repeated_output_reads_are_counted_and_sited_at_the_second_one() -> None:
    finding = _only("""
def test_a(capsys):
    capsys.readouterr()
    run()
    capsys.readouterr()
    capsys.readouterr()
""")

    assert (finding.code, finding.site.line, finding.site.function) == ("VX202", 5, "test_a")
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

    assert _codes(source) == ["VX205"]


def test_the_log_attributes_with_no_counterpart_are_reported() -> None:
    source = """
def test_a(caplog):
    assert caplog.text
    assert caplog.record_tuples
    assert caplog.get_records("call")
    caplog.handler.flush()
    caplog.clear()
"""

    assert _codes(source) == ["VX206"] * 5


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

    assert _codes(source) == ["VX210"]


def test_catching_a_cancellation_is_recognized_through_an_import_of_the_exception() -> None:
    source = """
import pytest
from asyncio import CancelledError
async def test_a():
    with pytest.raises(CancelledError):
        await task
"""

    assert _codes(source) == ["VX211"]


def test_approx_over_a_collection_or_an_array_is_reported_and_over_a_scalar_is_not() -> None:
    source = """
import numpy as np, pytest
def test_a():
    assert a == pytest.approx([1.0, 2.0])
    assert b == pytest.approx({"x": 1.0})
    assert c == pytest.approx(np.array([1.0]))
    assert d == pytest.approx(1.0, rel=1e-6)
"""

    assert _codes(source) == ["VX213"] * 3


def test_an_imperative_skip_is_reported_wherever_it_is_reached_and_a_mark_is_not() -> None:
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

    assert _codes(source) == ["VX214", "VX214"]


def test_an_import_time_skip_is_reported() -> None:
    source = """
import pytest
lxml = pytest.importorskip("lxml")
"""

    assert _codes(source) == ["VX215"]


def test_recording_warnings_is_reported_in_both_spellings() -> None:
    source = """
import pytest
def test_a():
    with pytest.warns(UserWarning):
        warn()
    with pytest.deprecated_call():
        old()
"""

    assert _codes(source) == ["VX216", "VX216"]


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

    assert _codes(source) == ["VX217"] * 3


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

    assert _codes(source) == ["VX218"] * 3


def test_every_call_on_the_mocker_fixture_is_reported() -> None:
    source = """
def test_a(mocker):
    mocker.patch("app.client")
    mocker.spy(app, "send")
"""

    assert _codes(source) == ["VX219", "VX219"]


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

    assert [f.code for f in found] == ["VX401"] * 4
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

    assert _codes(source) == ["VX402"] * 5


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

    assert _codes(source) == ["VX403"] * 4


def test_logging_level_and_handler_mutation_is_reported() -> None:
    source = """
import logging
def test_a():
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger("app").setLevel(logging.DEBUG)
    logging.getLogger("app").addHandler(handler)
    logging.root.handlers = []
"""

    assert _codes(source) == ["VX404"] * 4


def test_module_surgery_is_reported_however_it_is_spelled() -> None:
    source = """
import importlib, sys
def test_a():
    sys.modules["app"] = stub
    sys.modules.pop("app", None)
    del sys.modules["app.db"]
    importlib.reload(app)
"""

    assert _codes(source) == ["VX405"] * 4


def test_changing_the_working_directory_is_reported() -> None:
    source = """
import contextlib, os
def test_a():
    os.chdir("/srv")
    with contextlib.chdir("/srv"):
        pass
"""

    assert _codes(source) == ["VX406", "VX406"]


def test_interpreter_wide_settings_are_reported() -> None:
    source = """
import decimal, locale, sys
def test_a():
    locale.setlocale(locale.LC_ALL, "C")
    decimal.setcontext(ctx)
    sys.setrecursionlimit(5000)
"""

    assert _codes(source) == ["VX407"] * 3


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

    assert _codes(source) == ["VX408", "VX408"]


def test_driving_an_event_loop_is_reported() -> None:
    source = """
import asyncio
def test_a():
    asyncio.run(main())
    loop = asyncio.new_event_loop()
    loop.run_until_complete(main())
"""

    assert _codes(source) == ["VX409"] * 3


def test_a_blocking_call_in_an_async_body_is_reported() -> None:
    finding = _only("""
import time
async def test_a():
    time.sleep(0.1)
""")

    assert finding.code == "VX410"
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

    assert _codes(source) == ["VX410"]


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

    assert _codes(source) == ["VX411", "VX411"]


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

    assert _codes(source) == ["VX412"] * 4


def test_a_fixed_resource_is_reported_in_each_shape_it_is_recognized_by() -> None:
    source = """
def test_a(free_port):
    connect(host="db", Port=5432)
    connect(port=free_port)
    fetch("http://localhost:8000/health")
    open("/tmp/fixture.db")
"""

    assert _codes(source) == ["VX413"] * 3


# --- resolution, deduplication and whole files -------------------------------------------------


def test_an_aliased_import_resolves_to_the_construct_it_names() -> None:
    source = """
import pytest as pt
from pytest import raises
def test_a():
    box = raises(ValueError)
    pt.importorskip("lxml")
"""

    assert _codes(source) == ["VX210", "VX215"]


def test_one_finding_survives_per_code_line_and_function() -> None:
    source = """
def engine(request):
    return (request.node, request.session)
"""

    assert _codes(source) == ["VX015"]


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
    assert [(f.code, f.site.file) for f in scan.findings] == [("VX406", "tests/test_env.py")]


def test_scanning_skips_a_path_that_is_not_there(tmp_path: Path) -> None:
    scan = sources.scan([tmp_path / "gone.py"], root=tmp_path)

    assert (scan.files, scan.findings, scan.unparsed) == (0, (), ())


def test_a_file_that_will_not_parse_costs_its_own_findings_and_no_others(tmp_path: Path) -> None:
    broken = _write(tmp_path, "conftest.py", "def broken(:\n")
    fine = _write(tmp_path, "test_ok.py", "import os\ndef test_a():\n    os.chdir('/srv')\n")

    scan = sources.scan([broken, fine], root=tmp_path)

    assert (scan.unparsed, scan.files) == (("conftest.py",), 1)
    assert [f.code for f in scan.findings] == ["VX406"]


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

    assert [f.code for f in scan.findings] == ["VX216", "VX406"]


def test_scanning_reads_each_path_once(tmp_path: Path) -> None:
    path = _write(tmp_path, "test_dup.py", "import os\ndef test_a():\n    os.chdir('/srv')\n")

    scan = sources.scan([path] * 2, root=tmp_path)

    assert scan.files == 1
    assert len(scan.findings) == 1
