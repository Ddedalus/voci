"""`migration-report.md`: an audit as a document, read top to bottom and then forwarded.

The verdict leads — how much of the suite converts untouched, and what share of it ends up running
serially — then comes everything that needs a person: what will not convert, what converts with a
caveat, what costs concurrency, and what the configuration and installed plugins imply. Last comes
what the scan could not see, so the numbers are read with their blind spots in view. Each finding
is filed once, and a section with nothing under it is left out.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from velox_migrate.audit.findings import Audit, Finding, ordered
from velox_migrate.matrix import Area, Construct, Disposition

# Enough sites to recognize a pattern; past this a reader is counting, not reading.
SITE_CAP = 20


def markdown(audit: Audit) -> str:
    """The whole report as one string ending in a single newline."""
    blocks = [
        *_opening(audit),
        *_verdict(audit),
        *_decisions(audit),
        *_caveats(audit),
        *_hazards(audit),
        *_rewiring(audit),
        *_configuration(audit),
        *_blind_spots(audit),
    ]
    return "\n\n".join(blocks) + "\n"


def write(audit: Audit, path: str | Path) -> None:
    """`audit` written to `path` as UTF-8 markdown."""
    Path(path).write_text(markdown(audit), encoding="utf-8")


def _opening(audit: Audit) -> list[str]:
    suite = audit.suite
    read = (
        f"This audit read {_plural(audit.summary.tests, 'test')} collected by pytest "
        f"{suite.pytest_version}, which is ground truth for the one environment the collection ran "
        f"in: a suite whose fixtures differ by platform or plugin version needs an audit per "
        f"environment."
    )
    if audit.unparsed:
        read += (
            f" {_plural(len(audit.unparsed), 'source file')} could not be read, so every count "
            f"taken from a test body is a lower bound."
        )
    return [f"# Migrating {suite.rootpath} to velox", read]


def _verdict(audit: Audit) -> list[str]:
    suite, totals = audit.suite, audit.summary
    rows = [
        ("Collected", str(totals.tests), ""),
        ("Converts untouched", str(totals.clean_tests), _percent(totals.clean_percent)),
        ("Converts with a review marker", str(totals.marker_tests), ""),
        ("Carries a concurrency hazard", str(totals.hazard_tests), ""),
        ("Blocked", str(totals.blocked_tests), _percent(totals.blocked_percent)),
        (
            "**Runs serially**",
            f"**{totals.serialized_tests}**",
            f"**{_percent(totals.serialized_percent)}**",
        ),
    ]
    blocks = ["## The verdict", _table(("Outcome", "Tests", "Share"), rows)]

    notes = []
    review = totals.marker_tests + totals.hazard_tests
    if review:
        notes.append(
            f"Needs a decision: {review} of {totals.tests} tests — {totals.marker_tests} with a "
            f"review marker, {totals.hazard_tests} with a concurrency hazard."
        )
    if totals.blocked_tests:
        notes.append(
            f"Blocked: {totals.blocked_tests} of {totals.tests} tests "
            f"({_percent(totals.blocked_percent)}) have no conversion path as they stand."
        )
    if notes:
        blocks.append(" ".join(notes))

    wiring = [
        f"- {_plural(totals.tests, 'test')} in {_plural(suite.test_files, 'file')}, "
        f"{suite.async_tests} of them `async def`",
        f"- {_plural(suite.fixtures, 'fixture')} defined in the suite, plus "
        f"{suite.plugin_fixtures} from pytest and installed plugins",
        f"- {_plural(suite.conftests, 'conftest directory', 'conftest directories')}, "
        f"{_plural(suite.overrides, 'override chain')} against a specialization budget of "
        f"{audit.budget}, {_plural(suite.autouse_nodes, 'autouse declaration')}",
    ]
    if suite.plugins:
        wiring.append(f"- Installed plugins: {', '.join(sorted(suite.plugins))}")
    blocks.append("\n".join(wiring))

    if not _anything_to_decide(audit):
        blocks.append(
            "Nothing in this suite needs a decision: every collected test converts as it stands."
        )
    return blocks


def _decisions(audit: Audit) -> list[str]:
    stoppers = (Disposition.UNSUPPORTED, Disposition.REFUSED)
    groups = _grouped(
        finding
        for finding in ordered(audit.findings)
        if finding.disposition in stoppers and finding.area is not Area.CONFIG
    )
    if not groups:
        return []
    blocks = [
        "## What needs a decision",
        "None of these convert on their own. Each one needs someone to decide what the suite "
        "should do instead, and until then the tests it touches stay under pytest.",
    ]
    for code, findings in groups:
        blocks += _group(code, findings)
    return blocks


def _caveats(audit: Audit) -> list[str]:
    groups = _grouped(
        finding
        for finding in ordered(audit.findings)
        if finding.disposition is Disposition.MARKER and finding.area is not Area.CONFIG
    )
    if not groups:
        return []
    blocks = [
        "## Converted with a caveat",
        "These convert and run. Each leaves a `VELOX-TODO` marker in the converted source at the "
        "point where velox's behaviour differs, so the caveat travels with the code and can be "
        "grepped for.",
    ]
    for code, findings in groups:
        blocks += _group(code, findings)
    return blocks


def _hazards(audit: Audit) -> list[str]:
    groups = _grouped(
        finding
        for finding in ordered(audit.findings)
        if finding.disposition is Disposition.HAZARD and finding.area is not Area.CONFIG
    )
    if not groups:
        return []
    totals = audit.summary
    blocks = [
        "## Concurrency hazards",
        f"These convert unchanged and mean something different once tests run at the same time. "
        f"Across the whole suite {_percent(totals.serialized_percent)} — {totals.serialized_tests} "
        f"of {totals.tests} tests — runs alone, and that share is what concurrency cannot speed "
        f"up.",
    ]
    # Most occurrences first: the biggest source of serial tests is the one worth fixing.
    for code, findings in sorted(groups, key=lambda group: (-len(group[1]), group[0])):
        blocks += _group(code, findings)
    return blocks


def _rewiring(audit: Audit) -> list[str]:
    groups = _grouped(
        finding
        for finding in ordered(audit.findings)
        if finding.disposition is Disposition.MECHANICAL and finding.area is not Area.CONFIG
    )
    if not groups:
        return []
    blocks = [
        "## What conversion rewires",
        "These convert without anyone reading the diff line by line. They are listed because the "
        "shape of the result is decided here: which fixtures are copied, where a declaration "
        "lands, and which tests end up scheduled alone.",
    ]
    for code, findings in groups:
        blocks += _group(code, findings)
    return blocks


def _configuration(audit: Audit) -> list[str]:
    groups = _grouped(finding for finding in ordered(audit.findings) if finding.area is Area.CONFIG)
    if not groups:
        return []
    blocks = [
        "## Plugins and configuration",
        "What the suite's settings and installed plugins amount to under velox, worst first. "
        "Unknown `[tool.velox]` keys are a hard error, so nothing here is written speculatively.",
    ]
    for code, findings in groups:
        blocks += _group(code, findings, label_disposition=True)
    return blocks


def _blind_spots(audit: Audit) -> list[str]:
    blocks = ["## What this audit cannot see"]
    if audit.blind_spots:
        blocks.append(
            "No dump and no parse finds these, so no number above counts them. Each is a way a "
            "converted suite can behave differently from the one pytest ran:"
        )
        blocks.append("\n".join(f"- {_blind_spot(row)}" for row in audit.blind_spots))
    if audit.unparsed:
        blocks.append(
            f"{_plural(len(audit.unparsed), 'source file')} could not be read. Every count taken "
            f"from a test body is a lower bound while that is true:"
        )
        blocks.append("\n".join(f"- {name}" for name in sorted(audit.unparsed)))
    return blocks if len(blocks) > 1 else []


def _group(code: str, findings: Sequence[Finding], *, label_disposition: bool = False) -> list[str]:
    row = findings[0].construct
    body = _plural(len(findings), "occurrence")
    affected = {nodeid for finding in findings for nodeid in finding.tests}
    if affected:
        body += f" affecting {_plural(len(affected), 'test')}"
    if label_disposition:
        body += f", {row.disposition}"
    body += f". {row.note}"
    if row.marker:
        body += f" Converted source carries `VELOX-TODO[{row.marker}]`."

    blocks = [f"### {code} — {row.subject}", body]
    if row.action:
        blocks.append(f"**Do:** {row.action}")
    blocks.append(_sites(findings))
    return blocks


def _sites(findings: Sequence[Finding]) -> str:
    seen = list(dict.fromkeys(_where(finding) for finding in findings))
    bullets = [f"- {line}" for line in seen[:SITE_CAP]]
    if len(seen) > SITE_CAP:
        bullets.append(f"- …and {len(seen) - SITE_CAP} more")
    return "\n".join(bullets)


def _where(finding: Finding) -> str:
    """A finding's location, falling back to its message for one about the suite as a whole."""
    return finding.message if finding.site.file is None else str(finding.site)


def _blind_spot(row: Construct) -> str:
    return f"**{row.subject}** — {row.note}" + (f" {row.action}" if row.action else "")


def _grouped(findings: Iterable[Finding]) -> list[tuple[str, tuple[Finding, ...]]]:
    """`findings` bucketed by code, each bucket in the order it was first reached."""
    buckets: dict[str, list[Finding]] = {}
    for finding in findings:
        buckets.setdefault(finding.code, []).append(finding)
    return [(code, tuple(bucket)) for code, bucket in buckets.items()]


def _anything_to_decide(audit: Audit) -> bool:
    return any(
        finding.disposition is not Disposition.MECHANICAL or finding.area is Area.CONFIG
        for finding in audit.findings
    )


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """A pipe table padded to even columns, first column left-aligned and the rest right."""
    grid = [[_cell(text) for text in row] for row in (header, *rows)]
    widths = [max(len(row[column]) for row in grid) for column in range(len(header))]
    rule = ["-" * widths[0]] + ["-" * (width - 1) + ":" for width in widths[1:]]
    lines = [_line(grid[0], widths), "| " + " | ".join(rule) + " |"]
    lines += [_line(row, widths) for row in grid[1:]]
    return "\n".join(lines)


def _line(cells: Sequence[str], widths: Sequence[int]) -> str:
    padded = [cells[0].ljust(widths[0])]
    padded += [cell.rjust(width) for cell, width in zip(cells[1:], widths[1:], strict=True)]
    return "| " + " | ".join(padded) + " |"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _percent(value: float) -> str:
    return f"{value:.1f}%"


def _plural(count: int, noun: str, plural: str | None = None) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {plural or noun + 's'}"
