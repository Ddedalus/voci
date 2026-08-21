"""Tests for velox_migrate.audit: the classification of a whole suite, end to end.

Every test runs against the checked-in dump from each supported pytest, against corpus suites
whose sources are in the tree — so what is asserted here is what a user's terminal would print,
joined from both halves of the audit rather than from either alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from velox_migrate import audit, model
from velox_migrate.audit import Audit
from velox_migrate.audit.findings import SEVERITY
from velox_migrate.matrix import Disposition

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
DUMPS = CORPUS / "dumps"
PYTEST_VERSIONS = ["8.4", "9.1"]

HAZARDS = "hazards_showcase"
FIXTURES = "fixtures_showcase"


def audit_of(suite: str, version: str, budget: int = audit.DEFAULT_BUDGET) -> Audit:
    ground_truth = model.load(DUMPS / f"{suite}-pytest-{version}.json")
    return audit.run(ground_truth, root=CORPUS / suite, budget=budget)


@pytest.fixture(params=PYTEST_VERSIONS, ids=[f"pytest{v}" for v in PYTEST_VERSIONS])
def version(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def hazards(version: str) -> Audit:
    return audit_of(HAZARDS, version)


@pytest.fixture
def fixtures(version: str) -> Audit:
    return audit_of(FIXTURES, version)


def codes(result: Audit) -> set[str]:
    return {finding.code for finding in result.findings}


def by_code(result: Audit, code: str) -> tuple:
    return tuple(finding for finding in result.findings if finding.code == code)


def test_the_two_supported_pytests_classify_a_suite_identically() -> None:
    # The extractor's version shims exist so that one audit reads both dumps. If the two ever
    # disagree, the difference is pytest's and the shim missed it.
    eight, nine = (audit_of(HAZARDS, version) for version in PYTEST_VERSIONS)

    assert codes(eight) == codes(nine)
    assert eight.summary.tests == nine.summary.tests
    assert eight.summary.serialized_tests == nine.summary.serialized_tests


def test_every_source_the_dump_names_is_read(hazards: Audit) -> None:
    assert hazards.unparsed == ()
    assert hazards.scanned_files == 6


def test_the_suite_is_described_by_what_its_tests_reach(hazards: Audit) -> None:
    suite = hazards.suite

    assert suite.tests == 47
    assert suite.test_files == 4
    assert suite.async_tests == 2
    assert suite.conftests == 2
    assert suite.overrides == 2


def test_the_dump_and_the_sources_are_both_read(hazards: Audit) -> None:
    # One code only collection can answer, one only a parse can, in a single audit.
    assert "VX006" in codes(hazards)
    assert "VX214" in codes(hazards)


@pytest.mark.parametrize(
    "code",
    [
        "VX003",  # a class-scoped fixture
        "VX006",  # an over-budget override
        "VX010",  # usefixtures on one test of a module
        "VX011",  # a literal getfixturevalue
        "VX012",  # a computed getfixturevalue
        "VX013",  # an unconditional finalizer
        "VX014",  # a conditional one
        "VX015",  # request.node
        "VX016",  # a custom command-line flag
        "VX019",  # setup_method
        "VX020",  # unittest.TestCase
        "VX022",  # a conftest hook
        "VX023",  # pytest_addoption
        "VX102",  # a per-case mark
        "VX103",  # a string skipif condition
        "VX105",  # a conditional xfail
        "VX106",  # xfail(run=False)
        "VX108",  # a per-test warning filter
        "VX113",  # two timeouts on one test
        "VX202",  # readouterr twice
        "VX203",  # capfd
        "VX205",  # caplog.set_level
        "VX208",  # tmpdir
        "VX210",  # a raises object stashed rather than entered or called
        "VX214",  # pytest.skip() as a statement
        "VX215",  # importorskip
        "VX216",  # recwarn and pytest.warns
        "VX217",  # a mock.patch decorator
        "VX218",  # a mock.patch context manager
        "VX221",  # approx over a generator
        "VX222",  # caplog.handler
        "VX305",  # addopts
        "VX307",  # filterwarnings in the ini file
        "VX308",  # log_cli
        "VX401",  # monkeypatch
        "VX402",  # an os.environ write
        "VX403",  # a warnings filter
        "VX404",  # a logging level
        "VX405",  # sys.modules surgery
        "VX406",  # os.chdir
        "VX407",  # the locale
        "VX409",  # a second event loop
        "VX410",  # a blocking call in a coroutine
        "VX411",  # module-level state
        "VX412",  # a seeded generator
        "VX413",  # a fixed resource
    ],
)
def test_the_corpus_suite_covers_the_construct(hazards: Audit, code: str) -> None:
    assert code in codes(hazards)


def test_a_blocking_call_is_only_a_hazard_inside_a_coroutine(hazards: Audit) -> None:
    blocking = by_code(hazards, "VX410")
    reached = {nodeid for finding in blocking for nodeid in finding.tests}

    assert any("test_blocking_call_in_a_coroutine" in nodeid for nodeid in reached)
    assert not any("test_blocking_call_in_a_plain_def" in nodeid for nodeid in reached)


def test_an_override_is_refused_by_its_fan_out_and_allowed_by_a_bigger_budget(
    version: str,
) -> None:
    refused = by_code(audit_of(HAZARDS, version), "VX006")
    (finding,) = refused

    assert finding.detail["fan_out"] == 7
    assert finding.disposition is Disposition.REFUSED
    assert codes(audit_of(HAZARDS, version, budget=10)) >= {"VX005"}
    assert "VX006" not in codes(audit_of(HAZARDS, version, budget=10))


def test_an_autouse_fixture_that_patches_the_process_serializes_what_it_reaches(
    hazards: Audit,
) -> None:
    # `patched_env` is autouse at the suite root and calls `monkeypatch.setenv`, so the hazard is
    # written once and inherited by every test that resolves it — which is the number that decides
    # adoption. Under `deep/` it is overridden by one that patches nothing, and those tests are
    # not charged for it.
    serialized = {
        nodeid for finding in hazards.findings if finding.serialized for nodeid in finding.tests
    }

    assert hazards.summary.serialized_tests == 45
    assert not any(nodeid.startswith("deep/") for nodeid in serialized)


def test_a_hazard_in_a_test_body_reaches_that_test_alone(hazards: Audit) -> None:
    in_a_test = [
        finding
        for finding in by_code(hazards, "VX402")
        if finding.site.function == "test_environment_write"
    ]

    assert in_a_test
    assert all(
        finding.tests == ("test_concurrency.py::test_environment_write",) for finding in in_a_test
    )


def test_a_hazard_in_an_autouse_fixture_reaches_the_tests_that_inherit_it(hazards: Audit) -> None:
    # `patched_env` clears an environment variable on teardown, and it is autouse at the root.
    (in_a_fixture,) = [
        finding for finding in by_code(hazards, "VX402") if finding.site.function == "patched_env"
    ]

    assert len(in_a_fixture.tests) == 45


def test_two_directories_declaring_the_same_autouse_name_are_told_apart(hazards: Audit) -> None:
    # Both conftests define `patched_env`, and the `velox.use(...)` line for each goes where its
    # own definition is, so a finding that sited both at one file would send someone to the
    # wrong file.
    sites = {
        (str(finding.detail["node"]), finding.site.file): finding
        for finding in by_code(hazards, "VX008")
        if finding.detail["fixture"] == "patched_env"
    }

    assert set(sites) == {(".", "conftest.py"), ("deep", "deep/conftest.py")}


def test_an_indirectly_parametrized_method_counts_its_own_cases(hazards: Audit) -> None:
    # `TestPlain` has a `test_it` too, and it is not one of these cases.
    (indirect,) = by_code(hazards, "VX007")

    assert indirect.site.function == "TestIndirect.test_it"
    assert indirect.tests == (
        "test_shapes.py::TestIndirect::test_it[mysql]",
        "test_shapes.py::TestIndirect::test_it[sqlite]",
    )


def test_a_finding_about_the_suite_names_no_test(hazards: Audit) -> None:
    (log_cli,) = [
        finding for finding in by_code(hazards, "VX308") if finding.detail["setting"] == "log_cli"
    ]

    assert log_cli.tests == ()
    assert hazards.summary.suite_findings >= 3


def test_the_ini_settings_read_are_the_ones_the_suite_wrote(hazards: Audit) -> None:
    written = {
        str(finding.detail["setting"])
        for finding in hazards.findings
        if "setting" in finding.detail
    }

    assert written == {
        "addopts",
        "filterwarnings",
        "log_cli",
        "log_cli_level",
        "markers",
        "xfail_strict",
    }
    assert "python_files" not in written


def test_every_test_falls_in_exactly_one_bucket(hazards: Audit) -> None:
    totals = hazards.summary

    assert (
        totals.clean_tests + totals.marker_tests + totals.hazard_tests + totals.blocked_tests
        == totals.tests
    )


def test_a_suite_with_no_hazards_reports_none(fixtures: Audit) -> None:
    assert by_code(fixtures, "VX401") == ()
    assert fixtures.summary.serialized_tests == 0
    assert fixtures.summary.hazard_tests == 0


def test_an_override_within_budget_is_mechanical(fixtures: Audit) -> None:
    (override,) = by_code(fixtures, "VX005")

    assert override.detail == {
        "fixture": "settings",
        "scope": "integration",
        "fan_out": 2,
        "budget": audit.DEFAULT_BUDGET,
        "duplicated": "engine",
    }


def test_an_autouse_fixture_is_reported_where_its_declaration_goes(fixtures: Audit) -> None:
    autouse = {finding.detail["node"]: finding for finding in by_code(fixtures, "VX008")}

    assert set(autouse) == {".", "integration"}
    assert len(autouse["integration"].tests) == 2
    assert autouse["."].site.file == "conftest.py"


def test_a_literal_dynamic_lookup_converts_and_a_computed_one_does_not(hazards: Audit) -> None:
    (literal,) = by_code(hazards, "VX011")
    (computed,) = by_code(hazards, "VX012")

    assert literal.disposition is Disposition.MECHANICAL
    assert computed.disposition is Disposition.REFUSED
    assert computed.site.function == "computed"


def test_a_hazard_in_a_fixture_reaches_the_tests_that_request_it(hazards: Audit) -> None:
    (chdir,) = [finding for finding in by_code(hazards, "VX401") if "chdir" in finding.message]

    assert chdir.site.file == "test_concurrency.py"
    assert len(chdir.tests) == 1


def test_the_blind_spots_are_the_constructs_no_audit_can_see(hazards: Audit) -> None:
    spots = {construct.code for construct in hazards.blind_spots}

    assert spots == {"VX414", "VX415", "VX416"}
    assert all(construct.action for construct in hazards.blind_spots)


def test_findings_are_ordered_worst_first(hazards: Audit) -> None:
    ranks = [SEVERITY.index(finding.disposition) for finding in hazards.findings]

    assert ranks == sorted(ranks)
    assert hazards.findings[0].disposition is Disposition.UNSUPPORTED
    assert hazards.findings[-1].disposition is Disposition.MECHANICAL


def test_a_source_file_the_root_does_not_hold_is_reported_as_unread(tmp_path: Path) -> None:
    ground_truth = model.load(DUMPS / f"{FIXTURES}-pytest-9.1.json")

    result = audit.run(ground_truth, root=tmp_path)

    assert result.unparsed == audit.sources_of(ground_truth)
    assert result.scanned_files == 0
