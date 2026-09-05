"""Command-line entrypoint for velox: parses arguments, resolves config, discovers and
collects tests, runs them, and prints the report.

Layers CLI flags over `[tool.velox]` config over built-in defaults (CLI wins), then
wires the result through `_discovery`, `_collect`, `_run`, and `_report` in that order.
`main` owns the process-global setup a run needs -- the assertion-rewrite import hook,
the rootdir entry on `sys.path`, environment variables from config -- and restores all
of it before returning, on every exit path.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import overload

from velox import __version__, _cache, _config, _warnings
from velox._assertions import rewrite as _rewrite
from velox._builtins import capture as _capture
from velox._collection import collect as _collect
from velox._collection import discovery as _discovery
from velox._collection import index as _index
from velox._collection import lastfailed as _lastfailed
from velox._collection import selection as _selection
from velox._collection import targets as _targets
from velox._report import color as _color
from velox._report import json_report as _json_report
from velox._report import terminal as _report
from velox._run import isolated as _isolated
from velox._run import run as _run
from velox._run import safety as _safety
from velox._wallclock import PROCESS_START


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
        help="Files, directories, or test ids (path.py::test_name, path.py::TestGroup::test_name, "
        "path.py::test_name[case]) to run, each read relative to the current directory, or to "
        "the rootdir if it names nothing there. Defaults to the configured testpaths, else the "
        "rootdir.",
    )
    parser.add_argument(
        "-k",
        dest="keywordexpr",
        metavar="EXPR",
        default=None,
        help="Run only tests whose id satisfies this boolean expression, e.g. 'users and not "
        "slow'. Each term is matched as a case-insensitive substring of the whole id -- path, "
        "test name and [case] suffix alike -- so -k users selects every test in "
        "tests/test_users.py. A term that isn't a bare identifier (has a dash, a dot or "
        "brackets) must be quoted, e.g. \"'test_create[admin]'\". A test that doesn't match is "
        "deselected, not skipped.",
    )
    parser.add_argument(
        "-m",
        dest="markexpr",
        metavar="EXPR",
        default=None,
        help="Run only tests whose @velox.tag(...) names satisfy this boolean expression, e.g. "
        "'slow and not flaky'. A tag name that isn't a bare identifier (has a dash or a dot) "
        "must be quoted, e.g. \"'smoke.fast'\". Tags not mentioned in EXPR count as absent. A "
        "test that doesn't match is deselected, not skipped; a skip-marked test is always "
        "skipped, regardless of EXPR.",
    )
    # Both read the run cache main writes at the end of every run. Spelled as pytest spells
    # them, since the muscle memory is the whole value of a two-letter flag.
    parser.add_argument(
        "--lf",
        "--last-failed",
        dest="last_failed",
        action="store_true",
        help="Run only the tests that failed, errored or timed out on the last run, plus every "
        "test in a file that failed to collect. Files holding none of them are not even "
        "imported. With nothing recorded -- a first run, or a run that went green -- the whole "
        "suite runs.",
    )
    parser.add_argument(
        "--ff",
        "--failed-first",
        dest="failed_first",
        action="store_true",
        help="Run the whole suite, with the tests that failed on the last run first. Unlike "
        "--lf this changes only the order, so a run that is still red says so within the first "
        "few results.",
    )
    # --assert and --rewrite-cache below both affect real behavior, not just help text.
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
    # Dispatches tests as asyncio.Tasks under a shared semaphore(N). type=int makes
    # argparse reject non-numeric input on its own; "positive" is checked by hand in
    # main, so it can report exit code 4 with a velox-styled message instead of
    # argparse's generic one.
    #
    # default=None, not DEFAULT_CONCURRENCY: main needs to tell "the user typed
    # --concurrency" apart from "argparse filled in a default" to apply CLI >
    # [tool.velox] > built-in default correctly -- see main's concurrency-resolution
    # comment for where None gets folded back to the real default.
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        metavar="N",
        help=f"Maximum number of tests running at once. Must be a positive "
        f"integer; 1 means exactly serial. Default: {_run.DEFAULT_CONCURRENCY}, or "
        f"[tool.velox] concurrency if set.",
    )
    # Wraps each test's setup+call in asyncio.timeout. Default is None (off): pytest
    # itself has no default test timeout either, so leaving this off keeps an existing
    # suite's behavior unchanged until the user opts in. <= 0 and non-finite values are
    # rejected by hand in main, same exit-4 style as --concurrency: asyncio.timeout(0)
    # neither raises nor means "no limit" -- it fires at the test's first suspension
    # point (or never, if it has none), which isn't a real, useful mode. None already
    # doubles as "unset" here, same trick as --concurrency above.
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Per-test setup+call budget, in seconds, overridable per test with "
        "@velox.timeout(...). A test that exceeds its budget is reported as TIMEOUT "
        "rather than FAILED/ERROR. Must be positive and finite. Default: no limit, or "
        "[tool.velox] timeout if set.",
    )
    # Unlike --timeout, this one is on by default: it never fails a test, and a suite that
    # has quietly gone serial behind one blocking call is exactly the kind of thing nobody
    # thinks to switch a diagnostic on for. 0 disables it. None doubles as "unset", same
    # trick as --concurrency above.
    parser.add_argument(
        "--loop-watchdog",
        dest="loop_watchdog",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Warn when the event loop has been blocked this long, naming the call holding it. "
        "A blocking call in an async test (or in a fixture) stalls every test at once. Pass 0 to "
        f"switch it off. Default: {_safety.DEFAULT_LOOP_WATCHDOG}, or [tool.velox] "
        f"loop_watchdog if set.",
    )
    # stdout/stderr are routed through a per-test Sink by default and shown only for
    # failing tests. -s/--capture=no disables that buffering for a live pass-through,
    # prefixed per line with the test id so concurrent output stays readable -- unlike
    # pytest's -s, this does not force serial.
    parser.add_argument(
        "--capture",
        choices=("no",),
        default=None,
        metavar="no",
        help="Set to 'no' (or pass -s) to pass captured stdout/stderr straight through to the "
        "real stream live, prefixed with the test id per line. Default: captured, and shown "
        "only for failing tests.",
    )
    parser.add_argument(
        "-s",
        dest="capture_s",
        action="store_true",
        help="Shorthand for --capture=no.",
    )
    # --serial is a shorthand rather than its own mode: everything it means is already
    # --concurrency=1, and main folds it into the same three-tier resolution so a
    # [tool.velox] concurrency doesn't quietly outrank a flag typed on the command line.
    parser.add_argument(
        "--serial",
        action="store_true",
        help="Shorthand for --concurrency=1: one test at a time, in collection order. The first "
        "step when a concurrent run behaves differently from a serial one.",
    )
    parser.add_argument(
        "--maxfail",
        type=int,
        default=None,
        metavar="N",
        help="Stop the run once N tests have failed: tests that haven't started are dropped, and "
        "tests still in flight are cancelled and reported as CANCELLED. Default: run everything.",
    )
    parser.add_argument(
        "-x",
        dest="exitfirst",
        action="store_true",
        help="Shorthand for --maxfail=1.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print a line per test as it finishes, on top of the per-file blocks.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Print one character per file instead of a block, and drop the startup header. "
        "Failure detail, the summary and every end-of-run section are printed regardless.",
    )
    parser.add_argument(
        "--durations",
        type=int,
        default=0,
        metavar="N",
        help="List the N slowest tests at the end of the run. Under concurrency the slowest "
        "test is what the wall clock can't drop below, so this is the list to read before "
        "tuning --concurrency. Default: 0, no such list.",
    )
    parser.add_argument(
        "-W",
        dest="filterwarnings",
        action="append",
        default=[],
        metavar="SPEC",
        help="Add a warning filter, as action:message:category:module:lineno -- "
        "'error', 'ignore::DeprecationWarning', 'error:.*legacy:UserWarning'. Repeatable; a "
        "later filter outranks an earlier one, and all of them outrank [tool.velox] "
        "filterwarnings. Every warning a run raises is reported either way; a filter decides "
        "which are silenced and which fail the test that raised them.",
    )
    parser.add_argument(
        "--collect-only",
        dest="collect_only",
        action="store_true",
        help="Print the id of every selected test, in the order they would run, and exit "
        "without running any of them.",
    )
    parser.add_argument(
        "--report-json",
        dest="report_json",
        type=Path,
        default=None,
        metavar="PATH",
        help="Write one JSON record of the run to PATH: an outcome, duration and failure reason "
        "per test, so a consumer reads the result as data instead of parsing this reporter's own "
        "output. Not written for --collect-only, which never runs anything to report on.",
    )
    # A fresh, numbered session root by default (see
    # _capture.DEFAULT_BASETEMP_RETENTION), or this override. Validated by hand in
    # main (_invalid_basetemp_argument, exit code 4) before it ever reaches
    # _capture.install's own shutil.rmtree -- the one "does real work" flag whose
    # failure mode is irreversible.
    parser.add_argument(
        "--basetemp",
        type=Path,
        default=None,
        metavar="DIR",
        help="Override where tmp_path/tmp_path_factory allocate. WARNING: this directory is "
        "cleared (removed and recreated) at the start of every run that uses it -- do not point "
        "it at anything you did not create for this purpose. Default: a fresh numbered "
        f"directory under the platform temp dir, keeping the last "
        f"{_capture.DEFAULT_BASETEMP_RETENTION} previous runs.",
    )
    return parser


def _default_test_roots(rootdir: Path | None = None) -> list[Path]:
    """Where velox looks when the user passes no paths and `[tool.velox] testpaths`
    isn't set: `rootdir/tests` if it exists, else `rootdir` itself. This is only the
    last, built-in-default tier -- `main` tries `args.paths` and `config.testpaths`
    first and falls back to this only when neither is set. `rootdir` defaults to
    `cwd()` for callers that don't have one to hand.
    """
    # Path() (".") not Path.cwd() when no rootdir is given: this must stay relative so
    # existing callers see the same relative results, not ones Path.cwd() would turn
    # absolute.
    base = rootdir if rootdir is not None else Path()
    tests_dir = base / "tests"
    if tests_dir.is_dir():
        return [tests_dir]
    return [base]


def _friendly_path(path: Path, *, max_up_hops: int = 2) -> str:
    """`path` relative to `cwd()` when that's reasonably close, else the absolute path.

    "Close" means at most `max_up_hops` `..` segments -- climbing out of cwd more than
    a couple of levels stops reading as "nearby" and starts reading as noise, where an
    absolute path at least tells the reader where they actually are. A path *under*
    cwd is always shown relative regardless of depth: it's still the same project tree.
    """
    try:
        rel = os.path.relpath(path, Path.cwd())
    except ValueError:
        # Windows: relpath can't cross drives, e.g. C:\ vs D:\.
        return str(path)
    if sum(1 for part in Path(rel).parts if part == "..") <= max_up_hops:
        return rel
    return str(path)


def _invalid_target_argument(targets: list[_targets.Target]) -> str | None:
    """The first usage error in an explicit `PATHS` list, or `None` if they all look
    usable: a path that doesn't exist, or a test id whose selector is empty or hangs off
    a directory rather than a file. A path that exists but matches no test files is a
    legitimate, if unusual, empty selection -- not a usage error, unlike a selector that
    matches no test (checked after collection, once there are ids to match against).
    """
    for target in targets:
        if not target.path.exists():
            return f"path does not exist: {str(target.path)!r}"
        if target.selector is None:
            continue
        if not target.selector:
            return f"test id has nothing after '::': {target.raw!r}"
        if target.path.is_dir():
            return (
                f"a test id names a test inside one file, and {str(target.path)!r} is a "
                f"directory: {target.raw!r}"
            )
    return None


def _reread_on_rootdir(targets: list[_targets.Target]) -> list[_targets.Target]:
    """`targets`, with any path that names nothing from the current directory read relative to
    the rootdir instead, where that names something.

    Test ids are rootdir-relative wherever velox prints them, so this is what lets one be pasted
    back as an argument from a directory that isn't the rootdir. The literal reading always
    wins, so an argument that already names something keeps meaning what it says.

    The rootdir here is the one a `[tool.velox]` table fixes, found by `_config.resolve`'s own
    upward search from the current directory and raising its `ConfigError` the same way. A run
    with no such table has a rootdir derived from the arguments themselves, which would make one
    argument's meaning depend on the others, so those runs are left alone. Searched at all only
    when some argument names nothing from here, which is the uncommon case.
    """
    if all(target.path.is_absolute() or target.path.exists() for target in targets):
        return targets
    config = _config.resolve([])
    if config.source is None:
        return targets
    rootdir = config.rootdir
    reread = []
    for target in targets:
        on_rootdir = rootdir / target.path
        keep = target.path.is_absolute() or target.path.exists() or not on_rootdir.exists()
        reread.append(target if keep else replace(target, path=on_rootdir))
    return reread


def _invalid_basetemp_argument(basetemp: Path | None) -> str | None:
    """The usage error in an explicit `--basetemp DIR`, or `None` if it looks safe
    enough to let `_capture.install` decide the rest.

    `_capture._resolve_basetemp_root` does an unguarded `shutil.rmtree` on whatever
    this resolves to, so this catches the path shapes that would make an ordinary typo
    catastrophic: empty, the current directory or any ancestor, the home directory, or
    the filesystem root. Conservative rather than exhaustive -- an existing directory that isn't
    obviously dangerous but also doesn't look like a previous velox basetemp is refused instead
    by `_capture.install`'s own marker-file check, which has to exist there anyway for direct
    callers that skip `main`.
    """
    if basetemp is None:
        return None
    resolved = basetemp.expanduser().resolve()
    if resolved == Path(resolved.anchor):
        return f"--basetemp must not be the filesystem root: {resolved}"
    try:
        home = Path.home().resolve()
    except RuntimeError:
        # No resolvable home directory (a minimal/sandboxed environment) -- nothing to
        # compare against, so this check is simply inapplicable.
        home = None
    if home is not None and resolved == home:
        return f"--basetemp must not be the home directory: {resolved}"
    cwd = Path.cwd().resolve()
    if resolved == cwd or resolved in cwd.parents:
        return (
            f"--basetemp must not be the current directory or one of its parents (it is "
            f"cleared before use): {resolved}"
        )
    return None


def _report_collection(
    *,
    ids: Sequence[str],
    skipped: Sequence[tuple[str, str]],
    deselected: int,
    errors: Sequence[_collect.CollectionError],
    color_enabled: bool,
) -> int:
    """`--collect-only`: every selected test's id in the order they would run, then what
    collection found besides them, and the exit code for a run that stopped here.

    Plain primitives rather than a `CollectionResult`, so this same function serves both a real
    `collect()` and `_index.answer`'s index-only one, which has no `TestRecord`s (no import ran
    to build them) -- only the ids, lines and skip reasons the index kept.

    The ids are printed bare, one per line, so the list pipes into another tool (or back
    into `velox` as arguments) without stripping anything. An id's path is relative to the
    rootdir, and so is an argument that names nothing from the current directory, so a run
    a `[tool.velox]` table gives a fixed rootdir takes its own ids back from any directory
    under it. Skips and collection errors are
    shown the way a real run shows them: `--collect-only` is how a suite is inspected
    before it runs, and a file that failed to import is exactly what such an inspection is
    looking for.
    """
    for test_id in ids:
        print(test_id)

    skipped_word = _color.paint("SKIPPED", _color.YELLOW, enabled=color_enabled)
    for skip_id, reason in skipped:
        reason_text = _color.paint(f"({reason})", _color.GRAY, enabled=color_enabled)
        print(f"{skip_id} {skipped_word} {reason_text}")

    error_word = _color.paint("COLLECTION ERROR", _color.RED, enabled=color_enabled)
    for error in errors:
        print(f"{error.path} {error_word}")
        print(error.message)

    error_count = len(errors)
    counts = _color.counts(
        (len(skipped), "skipped", _color.YELLOW),
        (deselected, "deselected", _color.GRAY),
        (error_count, "collection error" if error_count == 1 else "collection errors", _color.RED),
        enabled=color_enabled,
    )
    found = len(ids)
    total = _color.paint(str(found), _color.PRIMARY, enabled=color_enabled)
    collected_line = f"{total} test{'' if found == 1 else 's'} collected"
    print(" · ".join(part for part in (collected_line, counts) if part))

    # Spelled out rather than deferred to `_run.exit_code_for`, which reads an empty result
    # list as "nothing ran, so nothing was collected" -- true of a real run, false here,
    # where nothing running is the whole point.
    if errors:
        return 1
    if not ids and not skipped:
        return 5
    return 0


@overload
def _resolve_layered[T](cli_value: T | None, config_value: T | None, default: T) -> T: ...
@overload
def _resolve_layered[T](
    cli_value: T | None, config_value: T | None, default: None = None
) -> T | None: ...
def _resolve_layered(cli_value, config_value, default=None):
    """CLI > `[tool.velox]` > built-in default, spelled out once so every merged
    option applies the same three-tier rule.

    Overloaded so a non-`None` `default` lets the type checker narrow the result to
    `T` instead of `T | None` -- `effective_timeout`, which omits `default`, correctly
    keeps the `T | None` it needs, since no timeout is itself a real, meaningful value
    here, not an unset marker.
    """
    if cli_value is not None:
        return cli_value
    if config_value is not None:
        return config_value
    return default


def _save_collection_index(
    rootdir: Path,
    previous: _index.Index,
    found: _collect.CollectionResult,
    *,
    files: Sequence[Path],
    discovered: Sequence[Path],
    roots: Sequence[Path],
) -> None:
    """Persist `previous` updated with what this run's real collection (`found`, over `files`)
    established. Called once per run that actually collected -- `found` rather than whatever
    `--lf`/`--ff` narrowed or reordered it into, matching `_cache.merge`'s own use of it below:
    the index describes what a file holds, not what one particular invocation asked to see."""
    _index.save(
        rootdir,
        _index.refresh(
            previous, found, rootdir=rootdir, files=files, discovered=discovered, roots=roots
        ),
    )


def _report_warnings(rootdir: Path) -> None:
    """Print the warnings a run raised on its way to an exit that never reaches `Reporter`."""
    _report.print_warnings(
        [(_report.NO_TEST_RUNNING, _warnings.session_warnings())],
        stream=sys.stdout,
        rootdir=rootdir,
        color_enabled=_color.color_enabled(sys.stdout),
    )


def _parse_filters(
    config: _config.Config, from_cli: Sequence[str]
) -> tuple[str | None, tuple[_warnings.WarningFilter, ...]]:
    """The run's warning filters, `[tool.velox]`'s first and the `-W` ones after — or a usage
    error naming whichever of the two wrote the spec that couldn't be parsed."""
    configured = config.filterwarnings or ()
    try:
        return None, _warnings.parse_filters((*configured, *from_cli))
    except _warnings.FilterError as exc:
        source = f"{config.source} 'filterwarnings':" if exc.spec in configured else "-W"
        return f"{source} {exc}", ()


def main(argv: list[str] | None = None) -> int:
    # The process start, not the top of main(): interpreter startup, importing velox,
    # argument parsing, config resolution, discovery, and collection (importing every
    # test module) all happen before a single test runs, and a run that spends its time
    # there rather than executing should say so, not report a "wall" time that only
    # covers the fast part. What remains between this and `time velox` is the launcher
    # in front of the interpreter -- `uv run` and the console script -- which no timer
    # inside the process can see.
    wall_start = PROCESS_START

    parser = build_parser()
    args = parser.parse_args(argv)

    # argparse's choices= can't express "unless this other flag is set", so this
    # contradiction (--rewrite-cache with --assert=plain, which never touches the
    # cache) is checked by hand. Exit 4: usage error.
    if args.assert_mode == "plain" and args.rewrite_cache is not None:
        print(
            "velox: --rewrite-cache has no effect with --assert=plain "
            "(plain mode never touches the rewrite cache)",
            file=sys.stderr,
        )
        return 4

    # --basetemp is the one "does real work" flag whose failure mode is irreversible
    # (_capture.install eventually shutil.rmtrees it), so it's checked here, before
    # collection and rewrite-hook setup run for a call that was always going to fail.
    basetemp_problem = _invalid_basetemp_argument(args.basetemp)
    if basetemp_problem is not None:
        print(f"velox: {basetemp_problem}", file=sys.stderr)
        return 4

    # Both shorthands mean exactly one other flag's value, so the contradiction is worth
    # reporting rather than silently picking a winner -- a run that quietly ignored
    # --serial (or -x) would report the wrong thing about what it did.
    if args.serial and args.concurrency is not None and args.concurrency != 1:
        print(
            f"velox: --serial is --concurrency=1, and --concurrency={args.concurrency} was also "
            f"given",
            file=sys.stderr,
        )
        return 4
    # One says "run only these", the other "run everything, these first"; picking a winner
    # silently would misreport what the run was.
    if args.last_failed and args.failed_first:
        print(
            "velox: --lf runs only the last run's failures and --ff runs the whole suite with "
            "them first -- pass one or the other",
            file=sys.stderr,
        )
        return 4
    if args.exitfirst and args.maxfail is not None and args.maxfail != 1:
        print(
            f"velox: -x is --maxfail=1, and --maxfail={args.maxfail} was also given",
            file=sys.stderr,
        )
        return 4

    # Checked by hand rather than through argparse for the same reason --concurrency is:
    # exit code 4 with a velox-styled message instead of argparse's generic one.
    maxfail = 1 if args.exitfirst else args.maxfail
    if maxfail is not None and maxfail < 1:
        print(f"velox: --maxfail must be a positive integer, got {maxfail}", file=sys.stderr)
        return 4
    if args.durations < 0:
        print(
            f"velox: --durations must be zero or more, got {args.durations}",
            file=sys.stderr,
        )
        return 4

    targets = [_targets.parse_target(raw) for raw in args.paths]
    # Test ids are rootdir-relative wherever velox prints them, so an argument naming
    # nothing from the current directory gets that reading before anything else looks at
    # it: pasting an id `--collect-only` printed back as an argument is the point.
    try:
        targets = _reread_on_rootdir(targets)
    except _config.ConfigError as exc:
        print(f"velox: {exc}", file=sys.stderr)
        return 4

    # A typo'd path and a genuinely empty suite must not look the same: without this,
    # a bad path would silently walk to nothing and exit 5 "no tests collected",
    # indistinguishable from an honest empty selection.
    problem = _invalid_target_argument(targets)
    if problem is not None:
        print(f"velox: {problem}", file=sys.stderr)
        return 4
    # None unless some argument actually carried a `::` selector, which is what lets
    # collection skip id filtering entirely in the common case.
    id_selection = _targets.IdSelection.of(targets)

    # Compiled up front, before collection does any real work, so a malformed -m/-k
    # expression fails fast with a usage error rather than surfacing mid-collection.
    markexpr = None
    keywordexpr = None
    try:
        if args.markexpr is not None:
            markexpr = _selection.compile_tag_expression(args.markexpr)
        if args.keywordexpr is not None:
            keywordexpr = _selection.compile_keyword_expression(args.keywordexpr)
    except _selection.SelectionError as exc:
        print(f"velox: {exc}", file=sys.stderr)
        return 4

    # [tool.velox]-anchored upward search, stopping at the git root -- see
    # _config.resolve's own docstring for exactly where it starts and stops. A
    # malformed pyproject.toml/[tool.velox] table is always a usage error: never
    # silently fall back to defaults over a config the user wrote but velox can't honor.
    # The file part of a `path.py::test_name` argument is what anchors the search, the
    # same as a plain path does.
    try:
        config = _config.resolve([target.path for target in targets])
    except _config.ConfigError as exc:
        print(f"velox: {exc}", file=sys.stderr)
        return 4

    # CLI > [tool.velox] > built-in default, via one shared helper -- args.concurrency/
    # args.timeout are None exactly when the flag wasn't given (see build_parser's
    # comments on both). --serial joins the CLI tier: a flag typed on the command line
    # outranks [tool.velox] concurrency whichever of the two spellings was used.
    effective_concurrency = _resolve_layered(
        1 if args.serial else args.concurrency, config.concurrency, _run.DEFAULT_CONCURRENCY
    )
    effective_timeout = _resolve_layered(args.timeout, config.timeout)
    effective_watchdog = _resolve_layered(
        args.loop_watchdog, config.loop_watchdog, _safety.DEFAULT_LOOP_WATCHDOG
    )

    # Named by its actual source (the CLI flag, or the config file that set it) rather
    # than always saying --concurrency/--timeout -- a bad [tool.velox] concurrency
    # shouldn't point the user at a flag they never touched.
    if effective_concurrency < 1:
        source = (
            "--concurrency" if args.concurrency is not None else f"{config.source} 'concurrency'"
        )
        print(
            f"velox: {source} must be a positive integer, got {effective_concurrency}",
            file=sys.stderr,
        )
        return 4
    if effective_timeout is not None and not (
        math.isfinite(effective_timeout) and effective_timeout > 0
    ):
        source = "--timeout" if args.timeout is not None else f"{config.source} 'timeout'"
        print(
            f"velox: {source} must be a positive, finite number of seconds, got "
            f"{effective_timeout}",
            file=sys.stderr,
        )
        return 4
    # 0 is a real value here (the diagnostic off) rather than a rejected one, so only
    # negative and non-finite values are usage errors.
    if not math.isfinite(effective_watchdog) or effective_watchdog < 0:
        source = (
            "--loop-watchdog"
            if args.loop_watchdog is not None
            else f"{config.source} 'loop_watchdog'"
        )
        print(
            f"velox: {source} must be zero (off) or a positive, finite number of seconds, got "
            f"{effective_watchdog}",
            file=sys.stderr,
        )
        return 4

    # Both tiers, in precedence order rather than layered like the scalars above: warning
    # filters accumulate, and the last one to match a warning is the one that decides it,
    # so a `-W` on the command line simply follows what [tool.velox] already said. Parsed
    # further down, once rootdir is on sys.path: a spec names a warning category, which for a
    # class the suite defines itself is not importable before then.
    effective_filterwarnings = (*(config.filterwarnings or ()), *args.filterwarnings)

    # PATHS > configured testpaths > the rootdir. config.testpaths entries are written
    # relative to wherever [tool.velox] was declared, so they're resolved against
    # config.rootdir here, not cwd().
    # is not None, not truthiness: testpaths = [] is a real, if unusual, thing to write
    # and means "nothing" -- truthiness would silently run the built-in default instead.
    if targets:
        roots = [target.path for target in targets]
    elif config.testpaths is not None:
        roots = [config.rootdir / p for p in config.testpaths]
        # Mirrors _invalid_target_argument's reasoning for CLI paths: a typo'd testpaths
        # entry must not silently look like an honest empty selection either.
        for root, raw in zip(roots, config.testpaths, strict=True):
            if not root.exists():
                print(
                    f"velox: {config.source}: testpaths entry does not exist: {raw!r}",
                    file=sys.stderr,
                )
                return 4
    else:
        roots = _default_test_roots(config.rootdir)

    # Loaded whether or not this run reads it back: the merge at the end of main needs what
    # the previous run recorded about tests this one never reaches.
    last_run = _cache.load(config.rootdir)
    # Loaded up front for the same reason: a `--collect-only` run below may answer from it
    # without ever reaching `collect()`, and every other run still needs it to build the
    # updated index it writes back at the end.
    collection_index = _index.load(config.rootdir)
    # An empty cache leaves both flags meaning "the whole suite, in logical order", which is
    # what makes --lf safe to leave in a shell alias -- a first run, or one that went green,
    # runs everything rather than nothing.
    replay_last_failed = args.last_failed and not last_run.is_empty()
    replay_failed_first = args.failed_first and not last_run.is_empty()

    # Resolved and probed up front so a silent fallback to plain mode is visible
    # before a run commits to it -- a benchmark that silently fell back would be a
    # corrupted one. plan warns on stderr; the header line (when there is one -- see
    # AssertionSetup.header_line) goes to stdout with the report. rootdir, not roots:
    # the cache is a project-wide thing, not tied to whichever subset of it this run
    # happens to be testing.
    setup = _rewrite.plan(
        roots, mode=args.assert_mode, cache_dir=args.rewrite_cache, rootdir=config.rootdir
    )
    # -q drops the two header lines and nothing else: they describe how the run was set
    # up, which is exactly the part a quiet run is asking to do without. What the run
    # *found* is printed at every verbosity.
    verbosity = (1 if args.verbose else 0) - (1 if args.quiet else 0)
    if verbosity >= 0:
        header = setup.header_line()
        if header is not None:
            print(header)
        # Same transparency plan's own header line gives the assertion-rewrite decision
        # -- a run silently picking up config the user forgot was there is exactly the
        # kind of surprise this avoids. _friendly_path: this is usually a couple of
        # directories under cwd (or cwd itself), and the absolute form is just noise at
        # that distance.
        if config.source is not None:
            print(f"config: {_friendly_path(config.source)}")
        else:
            print("config: none")
        # Said out loud rather than left to be inferred from the test count: the difference
        # between "--lf ran four tests" and "--lf ran the suite" is the whole point of asking.
        if (args.last_failed or args.failed_first) and last_run.is_empty():
            flag = "--lf" if args.last_failed else "--ff"
            print(f"{flag}: nothing recorded -- running the whole suite")

    rootdir = config.rootdir

    # env_backup and the matching restore loop in this try's finally (not monkeypatch
    # -- this is production code) make repeated in-process main() calls safe: every
    # key env touches is restored to its pre-call value (or removed) on the way out,
    # so one main() call's config never leaks into the next.
    #
    # The first thing inside this try, ahead of _rewrite.install: putting the mutation
    # inside the same try/finally that restores it means the restore fires even if
    # _rewrite.install itself raises.
    env_backup = {key: os.environ.get(key) for key in config.env}
    os.environ.update(config.env)

    # rootdir goes on sys.path exactly once, here, before the first test module import
    # below, so a plain absolute import rooted at rootdir resolves via ordinary PEP
    # 420 namespace-package lookup -- no __init__.py required. Collection's own import
    # mechanism is untouched by this, so relative imports between test modules
    # (`from .conftest import x`) stay unsupported.
    # sys.path[0], not appended: matches pytest's prepend import-mode convention, so
    # the test tree's own sources shadow a same-named installed package.
    # str(rootdir), not the Path: sys.path holds strings, and comparing a Path against
    # it with `in` would never match, inserting a fresh duplicate on every main() call.
    # Only removed on the way out if this call is the one that added it.
    rootdir_str = str(rootdir)
    sys_path_inserted = rootdir_str not in sys.path
    if sys_path_inserted:
        sys.path.insert(0, rootdir_str)

    # Set here, not just inside the try below: if _rewrite.installed_hook() itself
    # raised, the finally's `if not hook_already_installed:` would otherwise hit an
    # unbound name. False is also the safer fallback value -- it makes finally attempt
    # an uninstall(), not skip one.
    hook_already_installed = False
    warnings_installed = False
    try:
        # Must be installed before any test module is imported below -- a module
        # already in sys.modules can't retroactively be rewritten. warn already
        # happened inside plan above, so this call is handed the decision it made
        # rather than re-probing the cache.
        #
        # Known cost, not fixed here: install walks every .py under roots for its own
        # file list, and discover_files below walks the same roots again for test
        # files specifically -- two full traversals per run. They want different
        # filters (all .py vs test_*.py/*_test.py), so unifying them means changing
        # install's signature to accept a pre-discovered file list.
        hook_already_installed = _rewrite.installed_hook() is not None
        _rewrite.install(roots, setup=setup, warn=False)
        # After the hook, and before the first test-module import below: resolving a filter's
        # category imports the module holding it, which for one of the suite's own must go
        # through the rewrite hook like any other, and a module that warns at import time warns
        # during collection, which this is what records.
        problem, session_filters = _parse_filters(config, args.filterwarnings)
        if problem is not None:
            print(f"velox: {problem}", file=sys.stderr)
            return 4
        warnings_installed = _warnings.install(session_filters)
        # config.test_file_patterns/config.ignore replace discover_files's own
        # defaults outright when set, not add to them -- a user who wants "the
        # defaults plus one more" repeats the defaults themselves. is not None, not
        # truthiness, for both: an explicit [] is a real, if unusual, thing to write
        # and means "none".
        patterns = (
            config.test_file_patterns
            if config.test_file_patterns is not None
            else _discovery.DEFAULT_TEST_FILE_PATTERNS
        )
        ignore_dirs = (
            frozenset(config.ignore)
            if config.ignore is not None
            else _discovery.DEFAULT_IGNORE_DIRS
        )
        files = _discovery.discover_files(roots, patterns=patterns, ignore_dirs=ignore_dirs)
        # Before collect, not after: not importing the files that hold nothing --lf would run
        # is where the flag's speed comes from. `files` stays exactly what collection was
        # handed, which is what the merge below reads as "settled by this run".
        discovered = files

        # A plain --collect-only -- no -k/-m/id/--lf/--ff narrowing it to something the index
        # doesn't track -- can answer from `collection_index` alone when every discovered file
        # is still fresh in it, skipping collect() and therefore every import it would have
        # done. Anything narrower falls through to a real collection exactly as before.
        if (
            args.collect_only
            and not replay_last_failed
            and not replay_failed_first
            and markexpr is None
            and keywordexpr is None
            and id_selection is None
        ):
            fast_answer = _index.answer(collection_index, files, rootdir=rootdir)
            if fast_answer is not None:
                color_enabled = _color.color_enabled(sys.stdout)
                status = _report_collection(
                    ids=fast_answer.ids,
                    skipped=fast_answer.skipped,
                    deselected=0,
                    errors=(),
                    color_enabled=color_enabled,
                )
                # Called for symmetry with every other exit through this function: this run
                # imported nothing, so in the ordinary case there is nothing recorded to print.
                _report_warnings(rootdir)
                return status

        if replay_last_failed:
            files = _lastfailed.candidate_files(files, last_run, rootdir=rootdir)
        collected = _collect.collect(
            files,
            rootdir=rootdir,
            tag_expr=markexpr,
            keyword_expr=keywordexpr,
            id_selection=id_selection,
            # The unnarrowed set, so --lf leaving a test module out doesn't turn its
            # `velox.use(...)` into a misplaced declaration. Nothing here is imported. Only
            # when narrowed: otherwise it is `files`, which the loop below walks anyway.
            collectible=discovered if replay_last_failed else (),
        )
        # After -k/-m/ids rather than instead of them: --lf narrows a selection the other
        # flags already made, so `velox --lf -k users` means both.
        #
        # What collection itself found is kept for the cache write at the end: `select` moves
        # the tests --lf wasn't asked for into `deselected`, and `vanished` reads a deselected
        # unexpanded test as "this run never built its cases, so its recorded ones stand" --
        # true of a -k/-m deselection, and false of --lf's own, whose skips it would otherwise
        # strand in the cache for good.
        found = collected
        if replay_last_failed:
            collected = _lastfailed.select(collected, last_run)
            # Otherwise the run below reports "0 tests" and exits 5, which reads as a suite
            # that collected nothing rather than as one holding none of what was recorded --
            # a run pointed somewhere else, or a failing test renamed since. Checked on the
            # selection rather than on the candidate files, since a file can survive the
            # narrowing and still contribute nothing to it.
            # At every verbosity, unlike the "nothing recorded" line above it: that one
            # describes how the run was set up, which -q asks to do without, while this one
            # is the whole of what the run found.
            if not collected.records and not collected.skipped and not collected.errors:
                print("--lf: no recorded failure is in this run's selection")
        elif replay_failed_first:
            collected = _lastfailed.reorder(collected, last_run)
        # Same reasoning as a path that doesn't exist, one level down: a mistyped test id
        # would otherwise select nothing and exit 5, indistinguishable from a file that
        # genuinely holds no tests.
        #
        # Every id collection produced counts as a match, not just the selected ones: a
        # `@velox.skip`-marked test is a normal thing to name, and an id that `-k`/`-m`
        # then deselects is an empty intersection the user asked for, not a typo. Skipped
        # entirely when a file failed to import, since the ids it would have contributed
        # are unknowable -- the traceback printed below is the real story there.
        #
        # collected.unexpanded is the part of that which stops short of its own `[case]` ids
        # (a skip, or a test -m excluded), so `test_role[admin]` naming a case of one of those
        # counts as a match rather than reading as a typo.
        #
        # Not under --lf: an id naming a test that passed last time matches nothing there by
        # design, and velox has no way to tell that apart from a typo.
        if id_selection is not None and not collected.errors and not replay_last_failed:
            missing = id_selection.unmatched(
                [record.id for record in collected.records]
                + [skipped.id for skipped in collected.skipped]
                + collected.deselected,
                unexpanded=collected.unexpanded,
                rootdir=rootdir,
            )
            if missing:
                print(
                    f"velox: no test matches {', '.join(repr(name) for name in missing)}",
                    file=sys.stderr,
                )
                _report_warnings(rootdir)
                return 4

        # Shared with reporter's own coloring (it resolves the same thing internally
        # for its own prints) so the COLLECTION ERROR and --maxfail lines below, which
        # main prints itself rather than through reporter, match its file blocks
        # instead of being colored by a different rule.
        color_enabled = _color.color_enabled(sys.stdout)

        if args.collect_only:
            _save_collection_index(
                rootdir, collection_index, found, files=files, discovered=discovered, roots=roots
            )
            status = _report_collection(
                ids=[record.id for record in collected.records],
                skipped=[(skip.id, skip.reason) for skip in collected.skipped],
                deselected=len(collected.deselected),
                errors=collected.errors,
                color_enabled=color_enabled,
            )
            # Collection is what imports every test module, so a module that warns at import
            # has warned by now -- and this is the only report this run will print.
            _report_warnings(rootdir)
            return status

        capture_passthrough = args.capture == "no" or args.capture_s
        # Populated by run_suite iff non-None -- see _builtins/capture.py's module docstring
        # for what can land here. Empty in the common case. Rendered by
        # reporter.finish below, not printed here directly, keeping every "what got
        # printed and in what order" decision in one place.
        unattributed: list[str] = []

        # Reporter groups TestResults (which carry no path of their own) back into
        # per-file blocks by walking collected.records itself -- handed to it
        # directly, not reduced to an {id: path} dict first, since a dict
        # comprehension keyed by id would silently collapse two records sharing an id
        # (an ordinary case: a factory-generated test repeats its id for every
        # instance it produces). See Reporter's own docstring for the full reasoning.
        reporter = _report.Reporter(
            records=collected.records,
            skipped=collected.skipped,
            capture_passthrough=capture_passthrough,
            stream=sys.stdout,
            verbosity=verbosity,
            durations=args.durations,
            rootdir=rootdir,
        )

        # Set by run_suite's own on_interrupt callback, from the loop, the first time a
        # Ctrl-C lands. Everything the run did find is still reported below; this only
        # decides what the last line says and which exit code goes with it.
        interrupted = False

        def on_interrupt() -> None:
            nonlocal interrupted
            interrupted = True

        results = _run.run_suite(
            collected.records,
            concurrency=effective_concurrency,
            timeout=effective_timeout,
            capture_passthrough=capture_passthrough,
            maxfail=maxfail,
            basetemp=args.basetemp,
            unattributed_output=unattributed,
            on_result=reporter.on_result,
            on_interrupt=on_interrupt,
            # 0 means "off" at the CLI; run_suite spells that None, and treats a
            # non-positive number the same way regardless.
            loop_watchdog=effective_watchdog or None,
            filterwarnings=effective_filterwarnings,
            # setup.mode/setup.cache_dir, not args.assert_mode/args.rewrite_cache: an
            # isolated test's subprocess must reproduce what this run actually decided
            # (a --assert=rewrite request can still fall back to plain), not re-derive
            # and re-warn about it once per isolated test.
            isolated=_isolated.IsolatedConfig(
                rootdir=rootdir,
                assert_mode=setup.mode,
                assert_cache_dir=setup.cache_dir,
                rewrite_roots=tuple(roots),
            ),
        )
        wall_clock = time.monotonic() - wall_start

        # Before this function prints anything of its own: a run stopped by --maxfail
        # leaves a file block unprinted, and -q leaves its line of characters unclosed.
        reporter.flush_pending()

        error_word = _color.paint("COLLECTION ERROR", _color.RED, enabled=color_enabled)
        for error in collected.errors:
            print(f"{error.path} {error_word}")
            print(error.message)

        # run_suite returns one result per test that ran -- a cancelled test included --
        # so anything collection handed it that isn't in there is a test the run stopped
        # before it ever started.
        not_run = len(collected.records) - len(results)
        stopped_by = "interrupted" if interrupted else "--maxfail"
        if interrupted:
            print(_color.paint("INTERRUPTED (Ctrl-C)", _color.YELLOW, enabled=color_enabled))
        elif not_run:
            print(
                _color.paint(
                    f"stopped after {maxfail} failed (--maxfail)",
                    _color.YELLOW,
                    enabled=color_enabled,
                )
            )

        # Every count the run ends on is reporter's to print, so the file blocks above and
        # the totals below can't drift into disagreeing about the same suite. Skips reach it
        # through its constructor, since it counts them per file too.
        reporter.finish(
            results,
            wall_clock=wall_clock,
            unattributed_output=unattributed,
            session_warnings=_warnings.session_warnings(),
            not_run=not_run,
            not_run_label=stopped_by,
            deselected=len(collected.deselected),
            collection_errors=len(collected.errors),
        )

        # 2, not what the partial results happen to add up to: an interrupted run never
        # got to the point of having a verdict, and exiting 0 because the tests that did
        # finish passed would let a Ctrl-C read as success in CI.
        exit_status = (
            2
            if interrupted
            else _run.exit_code_for(results, collected.errors, skipped=len(collected.skipped))
        )
        if args.report_json is not None:
            _json_report.write_report(
                args.report_json,
                records=collected.records,
                results=results,
                skipped=collected.skipped,
                collection_errors=collected.errors,
                rootdir=rootdir,
                exit_status=exit_status,
                wall_clock=wall_clock,
                session_warnings=_warnings.session_warnings(),
            )
        # Last, after everything this run had to say: a cache velox cannot write costs the
        # next --lf its ordering, and must not touch this one's output or exit code.
        resolved_rootdir = rootdir.resolve()
        attempted = {str(_collect.display_path(path, resolved_rootdir)) for path in files}
        # One computation used both ways round: what this run can settle is exactly what it may
        # record, so no error goes into the cache that no later run could take back out.
        answered = _lastfailed.settled_paths(attempted, rootdir=resolved_rootdir)
        errored = _lastfailed.error_paths(collected.errors, answered=answered)
        # Recorded paths this run establishes nothing will ever collect again: gone from
        # disk, or in a directory it walked and no longer discovered there. Nothing else is
        # in a position to take these out of the cache.
        # Skipped outright with nothing recorded: there is no entry for a walk to declare
        # dead, and resolving every discovered path to find that out is not free.
        gone: set[str] = set()
        if not last_run.is_empty():
            gone = _lastfailed.dead_paths(
                last_run,
                discovered={
                    str(_collect.display_path(path, resolved_rootdir)) for path in discovered
                },
                roots=roots,
                rootdir=resolved_rootdir,
            )
        # A file that collected tests was read, whatever else in it went wrong: one malformed
        # test does not make the ids beside it unknowable, and treating the file as unread
        # would leave a renamed sibling recorded for good.
        produced = {str(record.path) for record in found.records}
        produced |= {str(skip.path) for skip in found.skipped}
        # A CANCELLED test never got to say anything about the code under test, so it settles
        # nothing: without this, the very stop --lf exists to iterate through -- `-x`, or a
        # Ctrl-C -- would drop every failure it cut short. `vanished` is the other direction:
        # a recorded id a file collection fully read no longer has is settled by its absence,
        # there being no run left to settle it.
        settled_ids = {r.id for r in results if r.outcome is not _run.Outcome.CANCELLED}
        settled_ids |= {s.id for s in collected.skipped}
        settled_ids |= _lastfailed.vanished(
            last_run,
            found,
            known_files=_lastfailed.read_files(attempted, errored - produced) | gone,
        )
        _cache.save(
            rootdir,
            _cache.merge(
                last_run,
                failed=[r.id for r in results if r.outcome in _run.FAILING_OUTCOMES],
                errored=errored,
                settled_ids=settled_ids,
                settled_files=answered | gone,
            ),
        )
        _save_collection_index(
            rootdir, collection_index, found, files=files, discovered=discovered, roots=roots
        )
        return exit_status
    except KeyboardInterrupt:
        # A Ctrl-C run_suite's own handler didn't turn into a graceful stop: the second
        # one (the deliberate "abort now" path), or one that landed while this call was
        # still collecting. Nothing partial is worth printing at that point -- the run
        # was abandoned, not finished.
        print(file=sys.stdout, flush=True)
        print("velox: aborted (Ctrl-C)", file=sys.stderr)
        return 2
    finally:
        # main is called repeatedly in-process (this package's own test suite does
        # exactly that), and an embedding caller may too -- leaving the hook on
        # sys.meta_path after this call returns would leak global state into whatever
        # runs next. This must fire on every exit path, including an exception
        # bubbling out of collection or execution.
        #
        # Only torn down if this call is the one that put it there: install is a
        # documented no-op when a hook is already on sys.meta_path, so an embedder (or
        # a nested main()) that installed its own hook first must keep it.
        if not hook_already_installed:
            _rewrite.uninstall()
        # Same rule, and the same reason: `warnings.showwarning` is process-global, so a
        # nested main() leaves the outer call's shim in place for the outer call to remove.
        if warnings_installed:
            _warnings.uninstall()
        # Symmetric with hook_already_installed above: only remove what this call put
        # on sys.path, and only if it's still there.
        if sys_path_inserted:
            with contextlib.suppress(ValueError):
                sys.path.remove(rootdir_str)
        # Whatever this run did or didn't get to, the rewriter may have written bytecode
        # into the cache directory on its way there.
        _cache.ensure_gitignore(rootdir)
        # Symmetric with env_backup's own comment above: restores exactly the keys
        # this call touched, to exactly what they were before it touched them.
        for key, prev_value in env_backup.items():
            if prev_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev_value


if __name__ == "__main__":
    sys.exit(main())
