"""Tests for the audit's type-readiness pass: which fixtures state a return type, and what each
one that does not costs.

Run against the checked-in dumps, whose corpus suites are written without annotations — except
`typed_showcase` — so the worklist under test is the one a user of an unannotated suite would be
handed. Where a single annotated fixture is needed the dump's own definition is annotated in
place, which is cheaper than reaching for another suite; `typed_showcase` is for the cases that
need a real annotated suite read end to end.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from voci_migrate import audit, model, report
from voci_migrate.audit import Audit, TypeReadiness, Unannotated
from voci_migrate.model import GroundTruth

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
DUMPS = CORPUS / "dumps"
PYTEST_VERSIONS = ["8.4", "9.1"]

FIXTURES = "fixtures_showcase"
OVERRIDES = "overrides_showcase"
PARAMETRIZE = "parametrize_showcase"
HAZARDS = "hazards_showcase"


def ground_truth_of(suite: str, version: str) -> GroundTruth:
    return model.load(DUMPS / f"{suite}-pytest-{version}.json")


def audit_of(suite: str, version: str, ground_truth: GroundTruth | None = None) -> Audit:
    return audit.run(ground_truth or ground_truth_of(suite, version), root=CORPUS / suite)


def readiness_of(suite: str, version: str) -> TypeReadiness:
    return audit_of(suite, version).type_readiness


def annotating(ground_truth: GroundTruth, *names: str, returns: str = "Session") -> GroundTruth:
    """`ground_truth` with every fixture named in `names` given a return annotation.

    Only the definitions are rewritten; the chains the items resolved keep the objects they had,
    which is what an annotation changing nothing about resolution looks like.
    """
    wanted = set(names) or {fixture.argname for fixture in ground_truth.fixture_defs.values()}
    return dataclasses.replace(
        ground_truth,
        fixture_defs={
            key: dataclasses.replace(fixture, returns=returns)
            if fixture.argname in wanted
            else fixture
            for key, fixture in ground_truth.fixture_defs.items()
        },
    )


def row(readiness: TypeReadiness, argname: str, file: str) -> Unannotated:
    found = [
        entry
        for entry in readiness.fixtures
        if entry.argname == argname and entry.site.file == file
    ]
    assert len(found) == 1, f"expected one {argname} in {file}, got {found}"
    return found[0]


@pytest.fixture(params=PYTEST_VERSIONS, ids=[f"pytest{v}" for v in PYTEST_VERSIONS])
def version(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_a_fixture_whose_factory_has_no_return_annotation_is_listed_with_where_it_is_written(
    version: str,
) -> None:
    entry = row(readiness_of(OVERRIDES, version), "engine", "conftest.py")

    assert entry.site.line == 23
    assert entry.site.function == "engine"
    assert entry.module == "conftest"
    assert str(entry.site) == "conftest.py:23 (engine)"


def test_a_fixture_whose_factory_carries_a_return_annotation_is_left_off_the_worklist(
    version: str,
) -> None:
    annotated = audit_of(
        OVERRIDES, version, annotating(ground_truth_of(OVERRIDES, version), "engine")
    ).type_readiness

    assert [entry.argname for entry in annotated.fixtures].count("engine") == 0
    assert annotated.annotated == 1
    assert annotated.total == readiness_of(OVERRIDES, version).total


def test_an_annotation_is_read_against_the_imports_of_the_module_it_was_written_in(
    version: str, tmp_path: Path
) -> None:
    # `-> t.Any` is `Any` only if `t` is `typing`, which only the fixture's own module says. Read
    # without it, this counted as annotated here while the conversion degraded it -- so a user was
    # told to annotate one set of fixtures and then lost another.
    root = tmp_path / "suite"
    root.mkdir()
    (root / "conftest.py").write_text(
        "import typing as t\n\n\ndef engine() -> t.Any:\n    ...\n", encoding="utf-8"
    )
    ground_truth = annotating(ground_truth_of(OVERRIDES, version), "engine", returns="t.Any")

    readiness = audit.run(ground_truth, root=root).type_readiness

    assert [entry.argname for entry in readiness.fixtures].count("engine") == 1


@pytest.mark.parametrize("returns", ["Any", "typing.Any", "Iterator", "Generator"])
def test_an_annotation_that_states_no_type_is_on_the_worklist_like_no_annotation(
    version: str, returns: str
) -> None:
    # `-> Any` is exactly as much information as no annotation and exactly as much work to fix,
    # so the worklist and the conversion report have to agree that it is nothing. The question is
    # `inference.infer`'s, asked once, rather than a second opinion about what counts.
    annotated = audit_of(
        OVERRIDES,
        version,
        annotating(ground_truth_of(OVERRIDES, version), "engine", returns=returns),
    ).type_readiness

    assert [entry.argname for entry in annotated.fixtures].count("engine") == 1
    assert annotated.annotated == 0


def test_annotating_a_fixture_removes_only_the_injections_that_fixture_carried(
    version: str,
) -> None:
    before = readiness_of(OVERRIDES, version)
    after = audit_of(
        OVERRIDES, version, annotating(ground_truth_of(OVERRIDES, version), "engine")
    ).type_readiness

    assert before.injections - after.injections == row(before, "engine", "conftest.py").injections


def test_the_worklist_is_ordered_by_how_many_injections_would_lose_their_type(
    version: str,
) -> None:
    readiness = readiness_of(OVERRIDES, version)

    counts = [entry.injections for entry in readiness.fixtures]
    assert counts == sorted(counts, reverse=True)
    assert readiness.fixtures[0].argname == "engine"


def test_fixtures_costing_the_same_are_ordered_by_where_they_are_written(version: str) -> None:
    readiness = readiness_of(OVERRIDES, version)

    tied = [entry for entry in readiness.fixtures if entry.injections == 2]
    assert [(entry.site.file, entry.site.line) for entry in tied] == [
        ("conftest.py", 18),
        ("integration/conftest.py", 12),
        ("integration/conftest.py", 17),
    ]


def test_two_fixtures_sharing_a_name_are_two_entries_written_in_two_places(version: str) -> None:
    readiness = readiness_of(OVERRIDES, version)

    settings = [entry for entry in readiness.fixtures if entry.argname == "settings"]
    assert [entry.site.file for entry in settings] == ["conftest.py", "integration/conftest.py"]


def test_a_fixture_nothing_requests_is_listed_last_rather_than_dropped(version: str) -> None:
    # It converts like any other and its parameters are typed like any other: what it costs today
    # is nothing, which the count says and the ordering acts on.
    readiness = readiness_of(OVERRIDES, version)

    assert row(readiness, "ledger", "conftest.py").injections == 0
    assert readiness.fixtures[-1].argname == "ledger"


def test_a_test_parametrized_into_several_cases_is_one_injection(version: str) -> None:
    # Both `backend` sites run twice: `test_backend_directly` is parametrized into two cases, and
    # `engine` requests `backend` for each of its own two. The parameter is written once each.
    readiness = readiness_of(PARAMETRIZE, version)

    assert row(readiness, "backend", "conftest.py").injections == 2
    assert row(readiness, "engine", "conftest.py").injections == 1


def test_a_fixture_requesting_another_is_an_injection_site_of_its_own(version: str) -> None:
    readiness = readiness_of(FIXTURES, version)

    # `settings` is requested by `engine` and by one test, and by nothing else.
    assert row(readiness, "settings", "conftest.py").injections == 2


def test_a_fixture_pytest_or_a_plugin_wrote_is_not_the_suites_to_annotate(version: str) -> None:
    # `legacy_dir` requests pytest's own `tmpdir`, which has no annotation and no line in this
    # suite to add one to.
    readiness = readiness_of(HAZARDS, version)

    named = {entry.argname for entry in readiness.fixtures}
    assert "legacy_dir" in named
    assert named.isdisjoint({"tmpdir", "monkeypatch", "request"})


def test_a_fixture_written_inside_a_class_is_named_by_its_qualified_name(version: str) -> None:
    readiness = readiness_of("classes_showcase", version)

    entry = row(readiness, "user", "test_classes.py")
    assert entry.site.function == "TestOverriding.user"


def test_the_totals_add_up_to_every_fixture_the_suite_defines(version: str) -> None:
    ground_truth = ground_truth_of(FIXTURES, version)
    readiness = audit_of(FIXTURES, version, ground_truth).type_readiness

    assert readiness.annotated == 0
    assert readiness.total == len(readiness.fixtures)
    assert readiness.injections == sum(entry.injections for entry in readiness.fixtures)


def test_a_fully_annotated_suite_has_nothing_on_the_worklist(version: str) -> None:
    readiness = audit_of(
        FIXTURES, version, annotating(ground_truth_of(FIXTURES, version))
    ).type_readiness

    assert readiness.fixtures == ()
    assert readiness.injections == 0
    assert readiness.annotated == readiness.total


def test_the_two_supported_pytests_read_the_same_worklist_from_a_suite() -> None:
    eight, nine = (readiness_of(OVERRIDES, version) for version in PYTEST_VERSIONS)

    assert [(entry.site.file, entry.site.line, entry.injections) for entry in eight.fixtures] == [
        (entry.site.file, entry.site.line, entry.injections) for entry in nine.fixtures
    ]


def test_the_report_names_every_unannotated_fixture_and_what_it_costs(version: str) -> None:
    result = audit_of(OVERRIDES, version)

    text = report.markdown(result)

    section = text.split("## Fixture return types", 1)[1].split("\n## ", 1)[0]
    assert "8 of 8 fixtures defined here state none" in section
    assert "the type is lost at 16 injected parameters" in section
    assert "- conftest.py:23 (engine) — 5 injections" in section
    assert "- conftest.py:33 (token) — 1 injection" in section
    assert "- conftest.py:38 (ledger) — nothing injects it" in section


def test_the_terminal_summary_quotes_the_worklist_it_wrote_to_the_report(version: str) -> None:
    line = [
        text
        for text in report.terminal(audit_of(OVERRIDES, version)).splitlines()
        if text.startswith("types:")
    ]

    assert line == [
        "types: 8 of 8 fixtures state no return type, costing 16 injected parameters "
        "— the report lists them worst first"
    ]


def test_neither_rendering_mentions_types_when_every_fixture_is_annotated(version: str) -> None:
    result = audit_of(FIXTURES, version, annotating(ground_truth_of(FIXTURES, version)))

    assert "## Fixture return types" not in report.markdown(result)
    assert "types:" not in report.terminal(result)


def test_the_payload_carries_the_worklist_for_tooling(version: str) -> None:
    block = report.payload(audit_of(OVERRIDES, version))["type_readiness"]

    assert block["fixtures"] == 8
    assert block["annotated"] == 0
    assert block["unannotated"] == 8
    assert block["injections"] == 16
    assert block["worklist"][0] == {
        "fixture": "engine",
        "module": "conftest",
        "file": "conftest.py",
        "line": 23,
        "function": "engine",
        "injections": 5,
    }
