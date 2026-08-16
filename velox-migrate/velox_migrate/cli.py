"""The `velox-migrate` command line.

`extract` is a convenience wrapper: it runs pytest with the extractor plugin loaded in the
current environment. Where that environment cannot be arranged, `velox_migrate/extractor.py`
copied next to the suite and loaded with `-p extractor` does the same job.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from velox_migrate import extractor, schema


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    # Unrecognized arguments are pytest's: `extract` is a wrapper around a pytest run, and a
    # suite generally needs some of its own flags to collect at all.
    args, passthrough = parser.parse_known_args(argv)
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
            "resolved to a JSON dump every later stage reads. Arguments this command does not "
            "recognize are passed to pytest unchanged."
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
        default=extractor.DEFAULT_OUT,
        metavar="PATH",
        help=f"where to write the dump (default: {extractor.DEFAULT_OUT})",
    )
    extract.set_defaults(run=_extract)
    return parser


def _extract(args: argparse.Namespace, passthrough: list[str]) -> int:
    # `--` is how a caller says the rest belongs to pytest. argparse leaves it in place, and
    # pytest reads it as "everything after this is a file path", so it is dropped here.
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

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
            "velox-migrate: pytest could not collect this suite, so there is no ground truth to "
            "migrate from. Fix collection first — the output above is pytest's own.",
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
