"""The `velox-migrate` command line.

`extract` is a convenience wrapper: it runs pytest, as a subprocess, with the extractor plugin
loaded. Where that environment cannot be arranged, `velox_migrate/extractor.py` copied next to
the suite and loaded with `-p extractor` does the same job. `audit` reads what `extract` wrote,
plus the suite's own sources, and writes the report.

Nothing here imports pytest. `extract` shells out to it, and every other stage reads the dump.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from velox_migrate import schema
from velox_migrate.audit import DEFAULT_BUDGET, sources_of
from velox_migrate.model import GroundTruth

# Kept in step with `extractor.DEFAULT_OUT`, which cannot be imported from: the extractor is a
# standalone file that imports nothing from this package.
DEFAULT_OUT = os.path.join(".velox-migrate", "ground-truth.json")

REPORT_NAME = "migration-report.md"
FINDINGS_NAME = "findings.json"


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

    audit = commands.add_parser(
        "audit",
        help="classify everything in the suite and report what migrating it would cost",
        description=(
            "Join the ground-truth dump with a static read of the suite's own sources, classify "
            "every construct against the support matrix, and write a report plus machine-readable "
            "findings. Reads only; converts nothing."
        ),
    )
    audit.add_argument(
        "-d",
        "--dump",
        default=DEFAULT_OUT,
        metavar="PATH",
        help=f"the ground-truth dump to read (default: {DEFAULT_OUT})",
    )
    audit.add_argument(
        "-r",
        "--root",
        default=None,
        metavar="PATH",
        help="where the suite's sources are (default: the rootdir the dump records, else here)",
    )
    audit.add_argument(
        "-o",
        "--out",
        default=None,
        metavar="DIR",
        help=f"where to write {REPORT_NAME} and {FINDINGS_NAME} (default: beside the dump)",
    )
    audit.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        metavar="N",
        help=(
            "how many fixtures one conftest override may cause to be duplicated before it is "
            "refused (default: %(default)s)"
        ),
    )
    audit.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="write the artifacts without printing the summary",
    )
    audit.set_defaults(run=_audit)

    convert = commands.add_parser(
        "convert",
        help="rewrite the suite as a velox one",
        description=(
            "Translate the suite's wiring, marks, bodies and configuration into their velox "
            "spelling, refusing anything that needs a decision and naming it in the source. "
            "Prints the plan and a diff; writes nothing without --write."
        ),
    )
    convert.add_argument(
        "-d",
        "--dump",
        default=DEFAULT_OUT,
        metavar="PATH",
        help=f"the ground-truth dump to read (default: {DEFAULT_OUT})",
    )
    convert.add_argument(
        "-r",
        "--root",
        default=None,
        metavar="PATH",
        help="where the suite's sources are (default: the rootdir the dump records, else here)",
    )
    convert.add_argument(
        "--write",
        action="store_true",
        help="apply the rewrite; without this the diff is printed and nothing changes",
    )
    convert.add_argument(
        "--disable",
        default="",
        metavar="CODES",
        help="comma-separated support-matrix codes whose rewrite rule to skip (e.g. VX101,VX204)",
    )
    convert.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        metavar="N",
        help="the override fan-out budget the audit behind this conversion uses (default: "
        "%(default)s)",
    )
    convert.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print the diff without the plan",
    )
    convert.set_defaults(run=_convert)
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

    # A failed run must not leave the previous run's dump behind: the next stage would read it
    # as though it described the suite as it stands now.
    Path(args.out).unlink(missing_ok=True)

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
    if completed.returncode not in schema.CLEAN_EXIT_STATUSES:
        print(
            f"velox-migrate: pytest exited {completed.returncode} without collecting this suite, "
            "so there is no ground truth to migrate from. Fix whatever the output above reports "
            "— it is pytest's own.",
            file=sys.stderr,
        )
        # A signal-killed subprocess reports a negative code, which an exit status cannot carry.
        return completed.returncode if completed.returncode > 0 else 1

    # The dump has to satisfy the same loader every later stage uses, and finding that out here
    # beats finding it out one command later.
    try:
        schema.load(args.out)
    except schema.DumpError as exc:
        print(f"velox-migrate: {exc}", file=sys.stderr)
        return 1
    return 0


def _audit(args: argparse.Namespace, passthrough: list[str]) -> int:
    from velox_migrate import audit, report

    loaded = _load(args)
    if loaded is None:
        return 1
    ground_truth, root = loaded

    result = audit.run(ground_truth, root=root, budget=args.budget)

    out = Path(args.out) if args.out else Path(args.dump).parent
    out.mkdir(parents=True, exist_ok=True)
    report_path, findings_path = out / REPORT_NAME, out / FINDINGS_NAME
    report.write_markdown(result, report_path)
    report.write_payload(result, findings_path)

    if not args.quiet:
        print(report.terminal(result))
        print(f"\nwrote {report_path} and {findings_path}")
    return 0


def _convert(args: argparse.Namespace, passthrough: list[str]) -> int:
    from velox_migrate import audit, convert, report

    loaded = _load(args)
    if loaded is None:
        return 1
    ground_truth, root = loaded

    result = convert.run(
        audit.run(ground_truth, root=root, budget=args.budget),
        ground_truth,
        root=root,
        disabled=[code.strip() for code in args.disable.split(",") if code.strip()],
    )

    if not args.quiet:
        print(report.plan(result))
        print()
    diff = result.edits.diff()
    print(diff if diff else "no change")

    if args.write:
        written = result.edits.apply(root)
        print(f"\nwrote {len(written)} file(s) under {root}")
    elif diff:
        print("\nnothing written; pass --write to apply")
    return 0


def _load(args: argparse.Namespace) -> tuple[GroundTruth, Path] | None:
    """The dump and the tree it describes, or `None` after reporting why neither can be read."""
    from velox_migrate import model

    try:
        ground_truth = model.load(args.dump)
    except schema.DumpError as exc:
        print(f"velox-migrate: {exc}", file=sys.stderr)
        return None
    root = _root_of(ground_truth, args.root)
    if root is None:
        print(
            f"velox-migrate: none of the {len(sources_of(ground_truth))} source files the "
            f"dump names are under {args.root or ground_truth.rootpath}, so there are no test "
            "bodies to read. Pass `--root` pointing at the suite.",
            file=sys.stderr,
        )
        return None
    return ground_truth, root


def _root_of(ground_truth: GroundTruth, chosen: str | None) -> Path | None:
    """Where the suite's sources are, or `None` if neither candidate holds any of them.

    A dump records the rootdir of the environment it was extracted in, which is a path on another
    machine as often as not, so the working directory is the fallback.
    """
    wanted = sources_of(ground_truth)
    candidates = [Path(chosen)] if chosen else [Path(ground_truth.rootpath), Path.cwd()]
    for candidate in candidates:
        if not wanted or any(Path(candidate, path).is_file() for path in wanted):
            return candidate
    return None


if __name__ == "__main__":
    raise SystemExit(main())
