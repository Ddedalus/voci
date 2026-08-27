"""Tests for velox_migrate.report: the JSON payload, the markdown report and the terminal summary.

Audits are built by hand here rather than scanned out of a suite, so a rendering is tested against
totals a reader can check by eye.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Sequence
from pathlib import Path

from velox_migrate import matrix
from velox_migrate.audit.findings import (
    Audit,
    Finding,
    Site,
    Suite,
    TypeReadiness,
    Unannotated,
    ordered,
    summarize,
)
from velox_migrate.report import (
    FINDINGS_VERSION,
    markdown,
    payload,
    terminal,
    write_markdown,
    write_payload,
)

SUITE = Suite(
    rootpath="/repo",
    pytest_version="9.1.1",
    environment={"sys_platform": "linux", "python_version": "3.13.1"},
    tests=10,
    test_files=4,
    async_tests=3,
    fixtures=21,
    plugin_fixtures=5,
    conftests=3,
    overrides=2,
    autouse_nodes=2,
    plugins=("pytest-mock", "pytest-asyncio"),
)

# One code per disposition, plus a second hazard and a config code, so every section of a report
# has something in it.
UNSUPPORTED = "VX020"
REFUSED = "VX014"
MARKED = "VX003"
HAZARD = "VX401"
SERIAL_HAZARD = "VX402"
MECHANICAL = "VX005"
CONFIG = "VX307"

CODE_ROW = re.compile(r"^VX\d{3}\s")

# A suite whose fixtures are all annotated, which is what most of these renderings assume.
TYPED = TypeReadiness()


def test_the_payload_carries_every_block_of_the_schema_it_names() -> None:
    data = payload(_mixed())

    assert data["findings_version"] == FINDINGS_VERSION
    assert set(data) == {
        "findings_version",
        "suite",
        "totals",
        "scan",
        "budget",
        "matrix",
        "type_readiness",
        "findings",
        "blind_spots",
    }


def test_the_payload_repeats_the_suite_the_audit_read() -> None:
    data = payload(_mixed())

    assert data["suite"]["rootpath"] == "/repo"
    assert data["suite"]["pytest_version"] == "9.1.1"
    assert data["suite"]["fixtures"] == 21
    assert data["suite"]["plugins"] == ["pytest-asyncio", "pytest-mock"]
    assert {field.name for field in dataclasses.fields(SUITE)} == set(data["suite"])


def test_the_payload_totals_are_the_numbers_the_summary_computed() -> None:
    audit = _mixed()

    totals = payload(audit)["totals"]

    assert totals["tests"] == 10
    assert totals["findings"] == len(audit.findings)
    assert totals["by_disposition"] == {
        "unsupported": 2,
        "refused": 1,
        "hazard": 2,
        "marker": 1,
        "mechanical": 2,
    }
    assert totals["clean_tests"] == 5
    assert totals["marker_tests"] == 1
    assert totals["hazard_tests"] == 1
    assert totals["blocked_tests"] == 3
    assert totals["convertible_tests"] == 7
    assert totals["serialized_tests"] == 1
    assert totals["serialized_percent"] == 10.0
    assert totals["suite_findings"] == 1


def test_the_payload_round_trips_through_json_unchanged() -> None:
    data = payload(_mixed())

    assert json.loads(json.dumps(data)) == data


def test_the_same_audit_renders_to_the_same_bytes_twice() -> None:
    audit = _mixed()

    assert _dumped(audit) == _dumped(audit)


def test_finding_order_does_not_depend_on_the_order_findings_arrived_in() -> None:
    findings = _findings()

    forwards = _audit(findings)
    backwards = _audit(list(reversed(findings)))

    assert _dumped(forwards) == _dumped(backwards)


def test_the_payload_matrix_holds_exactly_the_codes_the_findings_use() -> None:
    data = payload(_mixed())

    assert list(data["matrix"]) == sorted(
        {UNSUPPORTED, REFUSED, MARKED, HAZARD, SERIAL_HAZARD, MECHANICAL, CONFIG}
    )
    assert data["matrix"][REFUSED]["disposition"] == "refused"
    assert data["matrix"][REFUSED]["area"] == "wiring"
    assert data["matrix"][MARKED]["marker"] == "scope"
    assert data["matrix"][SERIAL_HAZARD]["serialized"] is True


def test_a_finding_carries_its_site_and_the_row_that_classifies_it() -> None:
    data = payload(_mixed())

    refused = next(item for item in data["findings"] if item["code"] == REFUSED)

    assert refused["file"] == "tests/test_a.py"
    assert refused["line"] == 12
    assert refused["function"] == "test_thing"
    assert refused["disposition"] == "refused"
    assert refused["subject"] == matrix.construct(REFUSED).subject
    assert refused["tests"] == ["tests/test_a.py::test_one"]


def test_the_payload_scan_block_names_the_files_that_could_not_be_read() -> None:
    audit = _audit(_findings(), unparsed=("tests/z.py", "tests/a.py"))

    scan = payload(audit)["scan"]

    assert scan["files"] == 5
    assert scan["unparsed"] == ["tests/a.py", "tests/z.py"]


def test_the_payload_names_the_constructs_no_scan_can_see() -> None:
    data = payload(_mixed())

    codes = [spot["code"] for spot in data["blind_spots"]]

    assert codes == [row.code for row in matrix.CONSTRUCTS if not row.detected]
    assert all(spot["note"] for spot in data["blind_spots"])


def test_the_written_payload_is_utf8_json_ending_in_one_newline(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"

    write_payload(_mixed(), path)

    text = path.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert not text.endswith("}\n\n")
    assert json.loads(text)["findings_version"] == FINDINGS_VERSION


def test_the_report_leads_with_the_share_of_the_suite_that_runs_serially() -> None:
    report = markdown(_mixed())

    verdict = report.split("## What needs a decision")[0]

    assert "10.0%" in verdict
    assert "serially" in verdict


def test_the_report_verdict_states_every_bucket_of_the_suite() -> None:
    report = markdown(_mixed())

    verdict = report.split("## What needs a decision")[0]

    assert re.search(r"\| Collected\s+\|\s+10 \|", verdict)
    assert re.search(r"\| Converts untouched\s+\|\s+5 \|\s+50\.0% \|", verdict)
    assert re.search(r"\| Blocked\s+\|\s+3 \|\s+30\.0% \|", verdict)
    assert "3 conftest directories" in verdict
    assert "pytest-mock" in verdict


def test_a_section_appears_for_every_disposition_the_audit_found() -> None:
    report = markdown(_mixed())

    assert "## What needs a decision" in report
    assert f"### {UNSUPPORTED} — " in report
    assert f"### {REFUSED} — " in report
    assert "## Converted with a caveat" in report
    assert "## Concurrency hazards" in report
    assert f"### {HAZARD} — " in report
    assert "## What conversion rewires" in report
    assert f"### {MECHANICAL} — " in report
    assert "## Plugins and configuration" in report
    assert f"### {CONFIG} — " in report


def test_every_finding_reaches_a_section_of_the_report() -> None:
    # A finding the audit raised and the report files nowhere is worse than no finding: the
    # numbers in the verdict count it and the reader never sees where it is.
    audit = _mixed()

    report = markdown(audit)

    for finding in audit.findings:
        assert f"### {finding.code} — " in report, finding.code


def test_a_caveat_names_the_marker_the_converted_source_carries() -> None:
    report = markdown(_mixed())

    caveats = report.split("## Converted with a caveat")[1]

    assert "`VELOX-TODO[scope]`" in caveats.split("## Concurrency hazards")[0]


def test_a_group_quotes_the_matrix_note_and_the_action_to_take() -> None:
    row = matrix.construct(REFUSED)
    assert row.action is not None

    report = markdown(_mixed())

    assert row.note in report
    assert row.action in report


def test_a_long_site_list_is_capped_with_a_remainder_line() -> None:
    crowd = [
        _finding(REFUSED, line=number, tests=(f"tests/test_a.py::test_{number}",))
        for number in range(1, 26)
    ]

    report = markdown(_audit(crowd, tests=25))

    assert report.count("- tests/test_a.py:") == 20
    assert "- …and 5 more" in report


def test_the_report_lists_the_fixtures_to_annotate_before_anything_is_converted() -> None:
    report = markdown(_audit(_findings(), readiness=_readiness(3, annotated=1)))

    section = report.split("## Fixture return types")[1].split("\n## ")[0]
    assert "3 of 4 fixtures defined here have no return annotation" in section
    assert "3 injected parameters lose their type" in section
    assert "- tests/conftest.py:0 (fixture_0) — 2 injections" in section
    assert "- tests/conftest.py:2 (fixture_2) — nothing injects it" in section


def test_the_report_says_nothing_about_types_when_every_fixture_is_annotated() -> None:
    report = markdown(_audit(_findings(), readiness=TypeReadiness(annotated=4)))

    assert "## Fixture return types" not in report
    assert "types:" not in terminal(_audit(_findings(), readiness=TypeReadiness(annotated=4)))


def test_a_long_worklist_is_capped_with_a_remainder_line() -> None:
    report = markdown(_audit(_findings(), readiness=_readiness(25)))

    assert report.count("- tests/conftest.py:") == 20
    assert "- …and 5 more" in report


def test_the_terminal_summary_quotes_the_worklist_in_one_line() -> None:
    summary = terminal(_audit(_findings(), readiness=_readiness(3, annotated=1)))

    assert "types: 3 of 4 fixtures have no return annotation, 3 injections lose" in summary


def test_the_payload_carries_the_worklist_in_the_order_the_report_prints_it() -> None:
    block = payload(_audit(_findings(), readiness=_readiness(3, annotated=1)))["type_readiness"]

    assert block == {
        "fixtures": 4,
        "annotated": 1,
        "unannotated": 3,
        "injections": 3,
        "worklist": [
            {
                "fixture": f"fixture_{number}",
                "module": "tests.conftest",
                "file": "tests/conftest.py",
                "line": number,
                "function": f"fixture_{number}",
                "injections": 2 - number,
            }
            for number in range(3)
        ],
    }


def test_a_clean_suite_is_reported_as_clean_in_one_line() -> None:
    report = markdown(_audit([]))

    assert "every collected test converts as it stands" in report
    assert "## What needs a decision" not in report
    assert "## Concurrency hazards" not in report
    assert "## Plugins and configuration" not in report


def test_no_heading_is_printed_with_nothing_under_it() -> None:
    for report in (markdown(_mixed()), markdown(_audit([]))):
        lines = report.splitlines()

        for position, line in enumerate(lines):
            if not line.startswith("#"):
                continue
            following = [rest for rest in lines[position + 1 :] if rest.strip()]
            assert following, f"{line!r} ends the report"
            assert not following[0].startswith("#"), f"{line!r} has nothing under it"


def test_the_report_surfaces_its_blind_spots_and_the_sources_it_could_not_read() -> None:
    audit = _audit(_findings(), unparsed=("tests/broken.py",))

    report = markdown(audit)

    gaps = report.split("## What this audit cannot see")[1]
    assert "test-order dependence" in gaps
    assert "fixture teardown timing" in gaps
    assert "tests/broken.py" in gaps
    assert "lower bound" in report


def test_the_report_ends_in_exactly_one_newline() -> None:
    report = markdown(_mixed())

    assert report.endswith("\n")
    assert not report.endswith("\n\n")


def test_the_written_report_keeps_its_table_readable_as_raw_text(tmp_path: Path) -> None:
    path = tmp_path / "migration-report.md"

    write_markdown(_mixed(), path)

    table = [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("|")]
    assert len(table) == 8
    # Three columns, padded to one width, and no cell smuggling a fourth divider in.
    assert all(line.count("|") == 4 for line in table)
    assert len({len(line) for line in table}) == 1


def test_the_terminal_summary_stays_within_a_screenful() -> None:
    audit = _audit(
        _every_construct(),
        tests=200,
        unparsed=("tests/a.py", "tests/b.py", "tests/c.py", "tests/d.py"),
    )

    summary = terminal(audit)

    assert len(summary.splitlines()) <= 20
    assert "\x1b" not in summary
    assert not summary.endswith("\n")


def test_the_terminal_summary_carries_the_counts_and_the_serial_share() -> None:
    summary = terminal(_mixed())

    assert "10 tests" in summary
    assert "5 clean (50.0%)" in summary
    assert "2 need review" in summary
    assert "3 blocked (30.0%)" in summary
    assert "1 of 10 tests run alone, 10.0% of the suite" in summary


def test_the_terminal_summary_lists_the_worst_offenders_first_and_stops() -> None:
    findings = []
    for rank, row in enumerate(_detected()[:12]):
        for occurrence in range(12 - rank):
            findings.append(
                _finding(
                    row.code,
                    line=occurrence + 1,
                    tests=(f"tests/test_{row.code}.py::test_{occurrence}",),
                )
            )

    summary = terminal(_audit(findings, tests=100))

    rows = [line for line in summary.splitlines() if CODE_ROW.match(line)]
    assert len(rows) == 8
    assert rows[0].startswith(_detected()[0].code)
    assert "…and 4 more constructs" in summary


def test_the_terminal_summary_names_the_files_it_could_not_read() -> None:
    audit = _audit(_findings(), unparsed=("tests/broken.py",))

    summary = terminal(audit)

    assert "tests/broken.py" in summary
    assert "lower bounds" in summary


def test_the_terminal_summary_says_how_many_constructs_no_scan_can_see() -> None:
    audit = _audit([])

    summary = terminal(audit)

    assert f"{len(audit.blind_spots)} constructs no scan can see" in summary


def _mixed() -> Audit:
    return _audit(_findings())


def _findings() -> list[Finding]:
    return [
        _finding(REFUSED, tests=("tests/test_a.py::test_one",)),
        _finding(
            UNSUPPORTED,
            file="tests/test_b.py",
            line=3,
            function="TestLegacy",
            tests=("tests/test_b.py::TestLegacy::test_x",),
        ),
        _finding(
            UNSUPPORTED,
            file="tests/test_b.py",
            line=40,
            function="TestOther",
            tests=("tests/test_b.py::TestOther::test_y",),
        ),
        _finding(
            MARKED,
            line=8,
            function="settings",
            tests=("tests/test_a.py::test_one", "tests/test_a.py::test_two"),
        ),
        _finding(
            HAZARD,
            file="tests/test_c.py",
            line=44,
            function="test_env",
            tests=("tests/test_c.py::test_env",),
        ),
        _finding(
            SERIAL_HAZARD,
            file="tests/test_c.py",
            line=45,
            function="test_env",
            tests=("tests/test_c.py::test_env",),
        ),
        _finding(
            MECHANICAL,
            file="tests/integration/conftest.py",
            line=4,
            function="settings",
            tests=("tests/test_a.py::test_one",),
        ),
        Finding(code=CONFIG, message="filterwarnings = error"),
    ]


def _finding(
    code: str,
    *,
    file: str = "tests/test_a.py",
    line: int = 12,
    function: str = "test_thing",
    tests: tuple[str, ...] = ("tests/test_a.py::test_thing",),
) -> Finding:
    return Finding(
        code=code,
        message=f"{code} at {file}:{line}",
        site=Site(file=file, line=line, function=function),
        tests=tests,
    )


def _audit(
    findings: Sequence[Finding],
    *,
    tests: int = 10,
    unparsed: tuple[str, ...] = (),
    readiness: TypeReadiness = TYPED,
) -> Audit:
    settled = ordered(findings)
    return Audit(
        suite=dataclasses.replace(SUITE, tests=tests),
        findings=settled,
        summary=summarize(settled, tests=tests),
        scanned_files=5,
        unparsed=unparsed,
        budget=5,
        type_readiness=readiness,
    )


def _readiness(count: int, *, annotated: int = 0) -> TypeReadiness:
    """`count` unannotated fixtures, the first injected most and the last injected not at all."""
    return TypeReadiness(
        fixtures=tuple(
            Unannotated(
                argname=f"fixture_{number}",
                module="tests.conftest",
                site=Site(file="tests/conftest.py", line=number, function=f"fixture_{number}"),
                injections=count - 1 - number,
            )
            for number in range(count)
        ),
        annotated=annotated,
    )


def _every_construct() -> list[Finding]:
    return [
        _finding(row.code, file=f"tests/test_{row.code}.py", tests=(f"t.py::test_{row.code}",))
        for row in _detected()
    ]


def _detected() -> list[matrix.Construct]:
    return [row for row in matrix.CONSTRUCTS if row.detected]


def _dumped(audit: Audit) -> str:
    return json.dumps(payload(audit), indent=1)
