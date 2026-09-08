"""The audit summary the CLI prints: the counts, the serial share, the loudest codes, the gaps.

A screenful for a suite of any size, so the codes are capped and each construct's subject is
clipped to keep a row on one line.
"""

from __future__ import annotations

from voci_migrate import matrix
from voci_migrate.audit.findings import Audit
from voci_migrate.report.markdown import plural

TOP_CODES = 8
_SUBJECT_WIDTH = 60
_NAMED_FILES = 3


def terminal(audit: Audit) -> str:
    """The summary as plain text, with no trailing newline."""
    groups = [_counts(audit), _codes(audit), _readiness(audit), _gaps(audit)]
    return "\n\n".join("\n".join(group) for group in groups if group)


def _counts(audit: Audit) -> list[str]:
    totals = audit.summary
    review = totals.marker_tests + totals.hazard_tests
    return [
        f"{totals.tests} tests   {totals.clean_tests} clean ({_percent(totals.clean_percent)})"
        f"   {review} need review"
        f"   {totals.blocked_tests} blocked ({_percent(totals.blocked_percent)})",
        f"serial: {totals.serialized_tests} of {totals.tests} tests run alone, "
        f"{_percent(totals.serialized_percent)} of the suite",
        f"{totals.findings} findings over {len(totals.by_code)} constructs, "
        f"{totals.suite_findings} about the suite itself",
    ]


def _codes(audit: Audit) -> list[str]:
    by_code = audit.summary.by_code
    top = sorted(by_code.items(), key=lambda entry: (-entry[1], entry[0]))[:TOP_CODES]
    if not top:
        return []
    rows = [(code, matrix.construct(code), count) for code, count in top]
    disposition = max(len(str(row.disposition)) for _, row, _ in rows)
    tally = max(len(str(count)) for _, _, count in rows)
    lines = [
        f"{code}  {str(row.disposition).ljust(disposition)}  {str(count).rjust(tally)}  "
        f"{_clip(row.subject)}"
        for code, row, count in rows
    ]
    if len(by_code) > len(top):
        lines.append(f"…and {len(by_code) - len(top)} more constructs")
    return lines


def _readiness(audit: Audit) -> list[str]:
    readiness = audit.type_readiness
    if not readiness.fixtures:
        return []
    return [
        f"types: {len(readiness.fixtures)} of {readiness.total} fixtures state no return "
        f"type, costing {plural(readiness.injections, 'injected parameter')} "
        f"— the report lists them worst first"
    ]


def _gaps(audit: Audit) -> list[str]:
    lines = []
    if audit.unparsed:
        lines.append(
            f"unread sources ({len(audit.unparsed)}): {_names(audit.unparsed)} "
            f"— counts from test bodies are lower bounds"
        )
    if audit.blind_spots:
        lines.append(f"{len(audit.blind_spots)} constructs no scan can see; the report names them")
    if audit.unclassified:
        lines.append(
            f"unclassified ({len(audit.unclassified)}): a fixture, test or class pytest resolved "
            f"that no scan located, touching "
            f"{plural(audit.summary.unclassified_tests, 'test')} excluded from clean — the "
            f"report names them"
        )
    return lines


def _names(unparsed: tuple[str, ...]) -> str:
    names = sorted(unparsed)
    shown = ", ".join(names[:_NAMED_FILES])
    rest = len(names) - _NAMED_FILES
    return shown if rest <= 0 else f"{shown} and {rest} more"


def _clip(subject: str) -> str:
    if len(subject) <= _SUBJECT_WIDTH:
        return subject
    return subject[: _SUBJECT_WIDTH - 1].rstrip() + "…"


def _percent(value: float) -> str:
    return f"{value:.1f}%"
