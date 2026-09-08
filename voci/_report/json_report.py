"""`--report-json PATH`: one JSON object recording what a run did, for a consumer that wants the
result as data rather than by parsing the terminal reporter.

`voci-migrate verify` is the first such consumer -- see `voci_migrate/verify/runners.py`, which
mirrors `REPORT_VERSION` the same way it already mirrors `voci_migrate.outcomes.OUTCOMES_VERSION`
(that module's own docstring explains why: a subprocess call across two trees, so nothing here can
be imported from the other side).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from voci import __version__
from voci._collection.collect import CollectionError, Skipped, TestRecord
from voci._run.run import TestResult
from voci._warnings import RecordedWarning

__all__ = ["REPORT_VERSION", "write_report"]

#: Bumped whenever the record's shape changes below.
REPORT_VERSION = 2


@dataclass(frozen=True, slots=True)
class _TestReportEntry:
    """One test's line in the report: what `-v` prints, minus the formatting."""

    id: str
    outcome: str
    duration: float
    failure_reason: str | None
    warnings: list[dict[str, Any]] = field(default_factory=list)


def write_report(
    path: Path,
    *,
    records: Sequence[TestRecord],
    results: Sequence[TestResult],
    skipped: Sequence[Skipped],
    collection_errors: Sequence[CollectionError],
    rootdir: Path,
    exit_status: int,
    wall_clock: float,
    session_warnings: Sequence[RecordedWarning] = (),
) -> None:
    """Write `path` the record of this run: one entry per test that ran plus one per test a skip
    mark kept out of the run entirely, ordered the way `records`/`skipped` collected them, so a
    diff between two reports for the same suite is a diff of outcomes rather than of reordering.

    `failure_reason` is `failure_summary` -- the short one-line "ExceptionType: message" already
    read straight off the exception, not the full traceback `-v` never printed either.

    Each entry carries the warnings that test raised, aggregated by warning and location the
    way the terminal summary groups them; `session_warnings` -- the ones raised with no test
    running -- are the report's own top-level `warnings`.

    `records` seeds nothing about which tests ran (`results` is already the run's own authoritative
    list -- same reasoning as `Reporter.finish`), but does give collection order to sort by: `id`
    alone is not always unique-and-ordered the way a factory-generated test's repeated id shows.
    """
    order = {record.id: index for index, record in enumerate(records)}
    entries = [
        _TestReportEntry(
            id=result.id,
            outcome=result.outcome.value,
            duration=result.duration,
            failure_reason=result.failure_summary,
            warnings=[asdict(warning) for warning in result.warnings],
        )
        for result in results
    ] + [
        _TestReportEntry(id=skip.id, outcome="skipped", duration=0.0, failure_reason=skip.reason)
        for skip in skipped
    ]
    entries.sort(key=lambda entry: order.get(entry.id, len(order)))

    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "runner": "voci",
        "runner_version": __version__,
        "rootpath": str(rootdir),
        "exit_status": exit_status,
        "wall_clock": wall_clock,
        "collection_errors": [str(error.path) for error in collection_errors],
        "warnings": [asdict(warning) for warning in session_warnings],
        "tests": [asdict(entry) for entry in entries],
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, sort_keys=False)
        handle.write("\n")
