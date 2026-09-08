"""`findings.json`: an audit as data, complete enough to act on without reading this tool.

Every finding names a support-matrix code, and the `matrix` block spells out each code the
findings use — its subject, its disposition, what it becomes and what to do about it — so a
consumer reads a finding's whole classification out of the file. The rendering is a pure function
of the audit, and collections with no inherent order are sorted, so one suite always renders to
one set of bytes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from voci_migrate import matrix
from voci_migrate.audit.findings import (
    SEVERITY,
    Audit,
    Finding,
    Suite,
    Summary,
    TypeReadiness,
    Unclassified,
    ordered,
    unclassified_ordered,
)
from voci_migrate.matrix import Construct

FINDINGS_VERSION = 3


def payload(audit: Audit) -> dict[str, Any]:
    """`audit` as JSON-ready data, under the schema `findings_version` names."""
    findings = ordered(audit.findings)
    codes = sorted({finding.code for finding in findings})
    return {
        "findings_version": FINDINGS_VERSION,
        "suite": _suite(audit.suite),
        "totals": _totals(audit.summary),
        "scan": {"files": audit.scanned_files, "unparsed": sorted(audit.unparsed)},
        "budget": audit.budget,
        "matrix": {code: _construct(matrix.construct(code)) for code in codes},
        "type_readiness": _type_readiness(audit.type_readiness),
        "findings": [_finding(finding) for finding in findings],
        "blind_spots": [_blind_spot(construct) for construct in audit.blind_spots],
        "unclassified": [_unclassified(row) for row in unclassified_ordered(audit.unclassified)],
    }


def write(audit: Audit, path: str | Path) -> None:
    """`audit` written to `path` as UTF-8 JSON ending in a newline."""
    write_json(payload(audit), path)


def write_json(data: Any, path: str | Path) -> None:
    """`data` written to `path` as UTF-8 JSON, indented and ending in a newline.

    The one place that convention (indent width, `ensure_ascii`) is decided, so `write` above and
    `verify/report.py::write_payload` -- the only other JSON artifact this tool writes from a
    process that can import the package -- cannot drift apart on it.
    """
    text = json.dumps(data, indent=1, ensure_ascii=False)
    Path(path).write_text(f"{text}\n", encoding="utf-8")


def _suite(suite: Suite) -> dict[str, Any]:
    return {
        "rootpath": suite.rootpath,
        "pytest_version": suite.pytest_version,
        "environment": {key: suite.environment[key] for key in sorted(suite.environment)},
        "tests": suite.tests,
        "test_files": suite.test_files,
        "async_tests": suite.async_tests,
        "fixtures": suite.fixtures,
        "plugin_fixtures": suite.plugin_fixtures,
        "conftests": suite.conftests,
        "overrides": suite.overrides,
        "autouse_nodes": suite.autouse_nodes,
        "plugins": sorted(suite.plugins),
    }


def _totals(summary: Summary) -> dict[str, Any]:
    return {
        "tests": summary.tests,
        "findings": summary.findings,
        # Worst disposition first, which is the order every rendering leads with.
        "by_disposition": {
            str(disposition): summary.by_disposition.get(disposition, 0) for disposition in SEVERITY
        },
        "by_code": {code: summary.by_code[code] for code in sorted(summary.by_code)},
        "clean_tests": summary.clean_tests,
        "marker_tests": summary.marker_tests,
        "hazard_tests": summary.hazard_tests,
        "unclassified_tests": summary.unclassified_tests,
        "blocked_tests": summary.blocked_tests,
        "convertible_tests": summary.convertible_tests,
        "suite_findings": summary.suite_findings,
        "serialized_tests": summary.serialized_tests,
        "serialized_percent": summary.serialized_percent,
        "clean_percent": summary.clean_percent,
        "blocked_percent": summary.blocked_percent,
    }


def _type_readiness(readiness: TypeReadiness) -> dict[str, Any]:
    return {
        "fixtures": readiness.total,
        "annotated": readiness.annotated,
        "unannotated": len(readiness.fixtures),
        "injections": readiness.injections,
        "worklist": [
            {
                "fixture": row.argname,
                "module": row.module,
                "file": row.site.file,
                "line": row.site.line,
                "function": row.site.function,
                "injections": row.injections,
            }
            for row in readiness.fixtures
        ],
    }


def _construct(construct: Construct) -> dict[str, Any]:
    return {
        "subject": construct.subject,
        "area": str(construct.area),
        "disposition": str(construct.disposition),
        "target": construct.target,
        "marker": construct.marker,
        "note": construct.note,
        "action": construct.action,
        "serialized": construct.serialized,
    }


def _finding(finding: Finding) -> dict[str, Any]:
    return {
        "code": finding.code,
        "disposition": str(finding.disposition),
        "area": str(finding.area),
        "subject": finding.subject,
        "message": finding.message,
        "marker": finding.marker,
        "serialized": finding.serialized,
        "file": finding.site.file,
        "line": finding.site.line,
        "function": finding.site.function,
        "tests": list(finding.tests),
        "detail": {key: finding.detail[key] for key in sorted(finding.detail)},
    }


def _blind_spot(construct: Construct) -> dict[str, Any]:
    return {
        "code": construct.code,
        "subject": construct.subject,
        "note": construct.note,
        "action": construct.action,
    }


def _unclassified(row: Unclassified) -> dict[str, Any]:
    return {
        "kind": row.kind,
        "name": row.name,
        "file": row.site.file,
        "line": row.site.line,
        "function": row.site.function,
        "tests": list(row.tests),
    }
