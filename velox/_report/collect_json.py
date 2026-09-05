"""`--co-json`: one JSON object printed to stdout describing what `--collect-only` found, for an
editor integration that wants the result as data instead of parsing the terminal reporter's plain-
text ids.

Takes the same "plain primitives" shape `cli._report_collection` does, for the same reason its own
docstring gives: a real `collect()` and `_index.answer`'s index-only one produce different types
(`TestRecord`/`Skipped`/`CollectionError` versus `_index.Answer`'s bare tuples), and this is the
call both normalize down to.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from velox import __version__

__all__ = ["COLLECT_VERSION", "TestLocation", "print_report"]

#: Bumped whenever the payload's shape changes below.
COLLECT_VERSION = 1


@dataclass(frozen=True, slots=True)
class TestLocation:
    """One collected test: its id and where it's defined."""

    id: str
    path: str
    lineno: int


def print_report(
    *,
    tests: Sequence[TestLocation],
    skipped: Sequence[tuple[str, str, str]],
    deselected: Sequence[str],
    errors: Sequence[tuple[str, str]],
    rootdir: Path,
) -> None:
    """Print `--co-json`'s one JSON object to stdout: `tests` in collection order, then what else
    collection found.

    `skipped` is `(id, path, reason)` and `errors` is `(path, message)` -- plain tuples rather than
    `_collect`'s own `Skipped`/`CollectionError`, so the same call serves the index's fast-path
    answer too, which never built either. `path` throughout is rootdir-relative, matching every id
    velox ever prints.
    """
    report: dict[str, Any] = {
        "collect_version": COLLECT_VERSION,
        "runner": "velox",
        "runner_version": __version__,
        "rootpath": str(rootdir),
        "tests": [asdict(test) for test in tests],
        "skipped": [{"id": id_, "path": path, "reason": reason} for id_, path, reason in skipped],
        "deselected": list(deselected),
        "collection_errors": [{"path": path, "message": message} for path, message in errors],
    }
    print(json.dumps(report, indent=1, sort_keys=False), file=sys.stdout)
