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
from pathlib import Path
from typing import overload

from velox import __version__, _config
from velox._assertions import rewrite as _rewrite
from velox._builtins import capture as _capture
from velox._collection import collect as _collect
from velox._collection import discovery as _discovery
from velox._collection import selection as _selection
from velox._collection import targets as _targets
from velox._report import color as _color
from velox._report import terminal as _report
from velox._run import isolated as _isolated
from velox._run import run as _run


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
        "path.py::test_name[case]) to run. Defaults to the configured testpaths, else the "
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
    # -x/--maxfail stop dispatching; they do not cancel what is already running (ROADMAP.md),
    # which is why the help says "stop starting" rather than "stop".
    parser.add_argument(
        "--maxfail",
        type=int,
        default=None,
        metavar="N",
        help="Stop starting new tests once N of them have failed. Tests already running are "
        "left to finish, so slightly more than N failures can be reported. Default: run "
        "everything.",
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
        "--collect-only",
        dest="collect_only",
        action="store_true",
        help="Print the id of every selected test, in the order they would run, and exit "
        "without running any of them.",
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


def _report_collection(collected: _collect.CollectionResult, *, color_enabled: bool) -> int:
    """`--collect-only`: every selected test's id in the order they would run, then what
    collection found besides them, and the exit code for a run that stopped here.

    The ids are printed bare, one per line, so the list pipes into another tool (or back
    into `velox` as arguments) without stripping anything. Skips and collection errors are
    shown the way a real run shows them: `--collect-only` is how a suite is inspected
    before it runs, and a file that failed to import is exactly what such an inspection is
    looking for.
    """
    for record in collected.records:
        print(record.id)

    skipped_word = _color.paint("SKIPPED", _color.YELLOW, enabled=color_enabled)
    for skipped in collected.skipped:
        reason = _color.paint(f"({skipped.reason})", _color.GRAY, enabled=color_enabled)
        print(f"{skipped.id} {skipped_word} {reason}")

    error_word = _color.paint("COLLECTION ERROR", _color.RED, enabled=color_enabled)
    for error in collected.errors:
        print(f"{error.path} {error_word}")
        print(error.message)

    errors = len(collected.errors)
    counts = _color.counts(
        (len(collected.skipped), "skipped", _color.YELLOW),
        (len(collected.deselected), "deselected", _color.GRAY),
        (errors, "collection error" if errors == 1 else "collection errors", _color.RED),
        enabled=color_enabled,
    )
    found = len(collected.records)
    total = _color.paint(str(found), _color.PRIMARY, enabled=color_enabled)
    collected_line = f"{total} test{'' if found == 1 else 's'} collected"
    print(" · ".join(part for part in (collected_line, counts) if part))

    # Spelled out rather than deferred to `_run.exit_code_for`, which reads an empty result
    # list as "nothing ran, so nothing was collected" -- true of a real run, false here,
    # where nothing running is the whole point.
    if collected.errors:
        return 1
    if not collected.records and not collected.skipped:
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


def main(argv: list[str] | None = None) -> int:
    # Started as the first thing main() does, not around run_suite alone: argument
    # parsing, config resolution, discovery, and collection (importing every test
    # module) all happen before a single test runs, and a run that spends its time
    # there rather than executing should say so, not report a "wall" time that only
    # covers the fast part. This is the closest velox's own process can get to what
    # `time velox` reports -- the remaining gap is interpreter/`uv` startup before
    # this line ever executes, which no timer inside the process can see.
    wall_start = time.monotonic()

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

    # A typo'd path and a genuinely empty suite must not look the same: without this,
    # a bad path would silently walk to nothing and exit 5 "no tests collected",
    # indistinguishable from an honest empty selection.
    targets = [_targets.parse_target(raw) for raw in args.paths]
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
        collected = _collect.collect(
            files,
            rootdir=rootdir,
            tag_expr=markexpr,
            keyword_expr=keywordexpr,
            id_selection=id_selection,
        )
        # Same reasoning as a path that doesn't exist, one level down: a mistyped test id
        # would otherwise select nothing and exit 5, indistinguishable from a file that
        # genuinely holds no tests.
        #
        # Every id collection produced counts as a match, not just the selected ones: a
        # `@velox.skip`-marked test is a normal thing to name, and an id that `-k`/`-m`
        # then deselects is an empty intersection the user asked for, not a typo. Skipped
        # entirely when a file failed to import, since the ids it would have contributed
        # are unknowable -- the traceback printed below is the real story there.
        if id_selection is not None and not collected.errors:
            missing = id_selection.unmatched(
                [record.id for record in collected.records]
                + [skipped.id for skipped in collected.skipped]
                + collected.deselected,
                rootdir=rootdir,
            )
            if missing:
                print(
                    f"velox: no test matches {', '.join(repr(name) for name in missing)}",
                    file=sys.stderr,
                )
                return 4

        # Shared with reporter's own coloring (it resolves the same thing internally
        # for its own prints) so the COLLECTION ERROR and --maxfail lines below, which
        # main prints itself rather than through reporter, match its file blocks
        # instead of being colored by a different rule.
        color_enabled = _color.color_enabled(sys.stdout)

        if args.collect_only:
            return _report_collection(collected, color_enabled=color_enabled)

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
        )

        results = _run.run_suite(
            collected.records,
            concurrency=effective_concurrency,
            timeout=effective_timeout,
            capture_passthrough=capture_passthrough,
            maxfail=maxfail,
            basetemp=args.basetemp,
            unattributed_output=unattributed,
            on_result=reporter.on_result,
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

        # run_suite returns one result per test that ran, so anything collection handed
        # it that isn't in there is a test --maxfail stopped before it started.
        not_run = len(collected.records) - len(results)
        if not_run:
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
            not_run=not_run,
            deselected=len(collected.deselected),
            collection_errors=len(collected.errors),
        )

        return _run.exit_code_for(results, collected.errors, skipped=len(collected.skipped))
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
        # Symmetric with hook_already_installed above: only remove what this call put
        # on sys.path, and only if it's still there.
        if sys_path_inserted:
            with contextlib.suppress(ValueError):
                sys.path.remove(rootdir_str)
        # Symmetric with env_backup's own comment above: restores exactly the keys
        # this call touched, to exactly what they were before it touched them.
        for key, prev_value in env_backup.items():
            if prev_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev_value


if __name__ == "__main__":
    sys.exit(main())
