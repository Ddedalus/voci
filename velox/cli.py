"""Command-line entrypoint for velox.

Placeholder: wires up the `velox` console script so the rest of the
tooling (uv, ruff, pyrefly, pytest) has something concrete to point at.
Real collection/scheduling/reporting lands in later commits.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from velox import __version__, _rewrite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="velox",
        description="Fast, concurrent test runner for Python.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Test paths to run (not yet implemented).",
    )
    # Assertion introspection is implemented (spec/07), so these two do real work already.
    parser.add_argument(
        "--assert",
        dest="assert_mode",
        choices=("rewrite", "plain"),
        default="rewrite",
        help="Assertion introspection mode. 'plain' skips the import hook; PEP 657 carets "
        "still render. Default: rewrite.",
    )
    parser.add_argument(
        "--rewrite-cache",
        metavar="DIR",
        default=None,
        help=f"Where rewritten .pyc files go. Defaults to a platform cache dir, or "
        f"${_rewrite.ENV_CACHE_DIR} if set. If it is unwritable, velox warns and falls "
        f"back to --assert=plain rather than silently paying the cold-import cost.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolved and probed up front so the cold-start guarantee (spec/07 §5) is visible before
    # a run commits to it — a benchmark that silently fell back to `plain` is a corrupted
    # benchmark. `plan` warns on stderr; the header line goes to stdout with the report.
    setup = _rewrite.plan(
        args.paths or [Path.cwd()],
        mode=args.assert_mode,
        cache_dir=args.rewrite_cache,
    )
    print(setup.header_line())

    parser.exit(1, f"velox {__version__}: not yet implemented ({args.paths!r})\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
