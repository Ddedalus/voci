"""The `velox-migrate` command line.

`extract` is a convenience wrapper: it runs pytest, as a subprocess, with the extractor plugin
loaded. Where that environment cannot be arranged, `velox_migrate/extractor.py` copied next to
the suite and loaded with `-p extractor` does the same job.

Nothing here imports pytest. `extract` shells out to it, and every other stage reads the dump.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys

from velox_migrate import schema

# Kept in step with `extractor.DEFAULT_OUT`, which cannot be imported from: the extractor is a
# standalone file that imports nothing from this package.
DEFAULT_OUT = os.path.join(".velox-migrate", "ground-truth.json")


def main(argv: list[str] | None = None) -> int:
    # Split on `--` before argparse sees it. Letting argparse sort the two groups out mixes them:
    # it claims flags it recognizes wherever they appear and files the rest as positionals, which
    # separates a pytest flag from its value.
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        separator = argv.index("--")
        argv, passthrough = argv[:separator], argv[separator + 1 :]

    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    return args.run(args, passthrough)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="velox-migrate", description="Migrate a pytest suite to velox."
    )
    commands = parser.add_subparsers(dest="command")

    extract = commands.add_parser(
        "extract",
        help="collect the suite under pytest and write its ground-truth dump",
        description=(
            "Collect the suite under pytest, without running any test, and write what pytest "
            "resolved to a JSON dump every later stage reads. Anything after `--` is passed to "
            "pytest unchanged."
        ),
    )
    extract.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="what to collect, passed through to pytest (default: pytest's own configuration)",
    )
    extract.add_argument(
        "-o",
        "--out",
        default=DEFAULT_OUT,
        metavar="PATH",
        help=f"where to write the dump (default: {DEFAULT_OUT})",
    )
    extract.set_defaults(run=_extract)
    return parser


def _extract(args: argparse.Namespace, passthrough: list[str]) -> int:
    if importlib.util.find_spec("pytest") is None:
        print(
            "velox-migrate: `extract` collects the suite with pytest, and this environment has "
            "none. Install pytest here, or copy velox_migrate/extractor.py into the environment "
            "the suite collects in and run it there.",
            file=sys.stderr,
        )
        return 1

    command = [
        sys.executable,
        "-m",
        "pytest",
        *args.paths,
        *passthrough,
        "-p",
        "velox_migrate.extractor",
        "--collect-only",
        "-q",
        "--extractor-out",
        args.out,
    ]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        print(
            f"velox-migrate: pytest exited {completed.returncode} without collecting this suite, "
            "so there is no ground truth to migrate from. Fix whatever the output above reports "
            "— it is pytest's own.",
            file=sys.stderr,
        )
        return completed.returncode

    # The dump has to satisfy the same loader every later stage uses, and finding that out here
    # beats finding it out one command later.
    try:
        schema.load(args.out)
    except schema.DumpError as exc:
        print(f"velox-migrate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
