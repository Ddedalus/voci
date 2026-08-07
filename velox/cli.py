"""Command-line entrypoint for velox.

Placeholder: wires up the `velox` console script so the rest of the
tooling (uv, ruff, pyrefly, pytest) has something concrete to point at.
Real collection/scheduling/reporting lands in later commits.
"""

from __future__ import annotations

import argparse
import sys

from velox import __version__


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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    parser.exit(1, f"velox {__version__}: not yet implemented ({args.paths!r})\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
