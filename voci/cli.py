"""Command-line entrypoint for voci: parses arguments, resolves config, discovers and
collects tests, runs them, and prints the report.

Layers CLI flags over `[tool.voci]` config over built-in defaults (CLI wins), then
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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO, overload

from voci import __version__, _cache, _config, _warnings
from voci._assertions import rewrite as _rewrite
from voci._builtins import capture as _capture
from voci._collection import collect as _collect
from voci._collection import discovery as _discovery
from voci._collection import index as _index
from voci._collection import lastfailed as _lastfailed
from voci._collection import selection as _selection
from voci._collection import targets as _targets
from voci._report import collect_json as _collect_json
from voci._report import color as _color
from voci._report import json_report as _json_report
from voci._report import terminal as _report
from voci._run import isolated as _isolated
from voci._run import run as _run
from voci._run import safety as _safety
from voci._wallclock import PROCESS_START
from voci._watch import run as _watch_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voci",
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
        "the rootdir if it names nothing there. Defaults to the configured testpaths, else to "
        "'tests' if there is one, else to the rootdir when [tool.voci] fixed it and the "
        "current directory otherwise.",
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
        help="Run only tests whose @voci.tag(...) names satisfy this boolean expression, e.g. "
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
    parser.add_argument(
        "--watch",
        dest="watch",
        action="store_true",
        help="Rerun after every change to a .py file under the selected paths, instead of "
        "exiting. The first run is whatever PATHS/-k/-m/--lf/--ff already say; every run after "
        "that applies --lf on top -- unless --lf or --ff was already given, which is left alone "
        "-- so a red run is what gets rerun until it's green, and a change with nothing left "
        "failing reruns the whole suite. Stops on Ctrl-C.",
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
        f"${_rewrite.ENV_CACHE_DIR} if set. If it is unwritable, voci warns and falls "
        f"back to --assert=plain rather than silently paying the cold-import cost.",
    )
    # Dispatches tests as asyncio.Tasks under a shared semaphore(N). type=int makes
    # argparse reject non-numeric input on its own; "positive" is checked by hand in
    # _prepare_run, so it can report exit code 4 with a voci-styled message instead of
    # argparse's generic one.
    #
    # default=None, not DEFAULT_CONCURRENCY: _prepare_run needs to tell "the user typed
    # --concurrency" apart from "argparse filled in a default" to apply CLI >
    # [tool.voci] > built-in default correctly -- see its own concurrency-resolution
    # comment for where None gets folded back to the real default.
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        metavar="N",
        help=f"Maximum number of tests running at once. Must be a positive "
        f"integer; 1 means exactly serial. Default: {_run.DEFAULT_CONCURRENCY}, or "
        f"[tool.voci] concurrency if set.",
    )
    # Wraps each test's setup+call in asyncio.timeout. Default is None (off): pytest
    # itself has no default test timeout either, so leaving this off keeps an existing
    # suite's behavior unchanged until the user opts in. <= 0 and non-finite values are
    # rejected by hand in _prepare_run, same exit-4 style as --concurrency: asyncio.timeout(0)
    # neither raises nor means "no limit" -- it fires at the test's first suspension
    # point (or never, if it has none), which isn't a real, useful mode. None already
    # doubles as "unset" here, same trick as --concurrency above.
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Per-test setup+call budget, in seconds, overridable per test with "
        "@voci.timeout(...). A test that exceeds its budget is reported as TIMEOUT "
        "rather than FAILED/ERROR. Must be positive and finite. Default: no limit, or "
        "[tool.voci] timeout if set.",
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
        f"switch it off. Default: {_safety.DEFAULT_LOOP_WATCHDOG}, or [tool.voci] "
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
    # [tool.voci] concurrency doesn't quietly outrank a flag typed on the command line.
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
        "later filter outranks an earlier one, and all of them outrank [tool.voci] "
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
        "--co-json",
        dest="co_json",
        action="store_true",
        help="Like --collect-only (and implies it), but print one JSON object to stdout instead "
        "of the plain id list: every selected test's id, file and definition line, plus what "
        "else collection found -- skips, deselections, and any file that failed to import. For "
        "an editor integration that wants the result as data. Nothing else goes to stdout during "
        "this run, including the usual startup header and any import-time warnings, which are "
        "printed to stderr instead so the one line of JSON stays parseable on its own.",
    )
    parser.add_argument(
        "--report-json",
        dest="report_json",
        type=Path,
        default=None,
        metavar="PATH",
        help="Write one JSON record of the run to PATH: an outcome, duration and failure reason "
        "per test, so a consumer reads the result as data instead of parsing this reporter's own "
        "output. Not written for --collect-only/--co-json, which never run anything to report "
        "on.",
    )
    # A fresh, numbered session root by default (see
    # _capture.DEFAULT_BASETEMP_RETENTION), or this override. Validated by hand in
    # _prepare_run (_invalid_basetemp_argument, exit code 4) before it ever reaches
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
    """Where voci looks when the user passes no paths and `[tool.voci] testpaths`
    isn't set: `rootdir/tests` if it exists, else `rootdir` itself. This is only the
    last, built-in-default tier -- `main` tries `args.paths` and `config.testpaths`
    first and falls back to this only when neither is set. `rootdir` defaults to
    `cwd()` for callers that don't have one to hand.

    `main` passes a rootdir only when `[tool.voci]` fixed one; without a table it passes
    nothing and this tier anchors at the current directory instead. Those used to be the same
    directory, and stopped being so once an unpinned rootdir started climbing to the project
    root (`_config.resolve`): a bare `voci` run from inside `tests/unit` still means "the tests
    here", not the whole suite the new rootdir can see.
    """
    # Path() (".") not Path.cwd() when no rootdir is given: this must stay relative so
    # existing callers see the same relative results, not ones Path.cwd() would turn
    # absolute.
    base = rootdir if rootdir is not None else Path()
    tests_dir = base / "tests"
    if tests_dir.is_dir():
        return [tests_dir]
    return [base]


def _watch_scope(args: argparse.Namespace) -> tuple[list[Path], frozenset[str]]:
    """A best-effort approximation of the roots and ignored directories `main`'s own
    resolution below would settle on -- good enough for `--watch` to know what to poll for
    changes, computed once, before the first of its runs.

    Deliberately looser than `main`'s own: a bad path or a broken `[tool.voci]` table here
    just means less gets watched (or the built-in default does), rather than failing the way a
    real run's validation does -- that validation, and its error message, still happen inside
    every iteration `--watch` actually runs, `_watch_scope` never being the thing that reports
    a usage error.
    """
    targets = [_targets.parse_target(raw) for raw in args.paths]
    # Same re-anchoring `main` itself does below, and for the same reason: a test id pasted
    # back from a previous run is rootdir-relative, and read literally from a subdirectory of a
    # project with a [tool.voci] table it would otherwise name a path that doesn't exist --
    # nothing `discover_files` would ever see a change under.
    with contextlib.suppress(_config.ConfigError):
        targets = _reread_on_rootdir(targets)
    try:
        config = _config.resolve([target.path for target in targets])
    except _config.ConfigError:
        return (
            [target.path for target in targets] or _default_test_roots(),
            _discovery.DEFAULT_IGNORE_DIRS,
        )
    ignore_dirs = (
        frozenset(config.ignore) if config.ignore is not None else _discovery.DEFAULT_IGNORE_DIRS
    )
    if targets:
        return [target.path for target in targets], ignore_dirs
    if config.testpaths is not None:
        return [config.rootdir / p for p in config.testpaths], ignore_dirs
    return _default_test_roots(config.rootdir), ignore_dirs


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

    Test ids are rootdir-relative wherever voci prints them, so this is what lets one be pasted
    back as an argument from a directory that isn't the rootdir. The literal reading always
    wins, so an argument that already names something keeps meaning what it says.

    The rootdir here is whichever `pyproject.toml` fixes one, found by `_config.resolve`'s own
    upward search from the current directory and raising its `ConfigError` the same way -- the
    `[tool.voci]` table where there is one, and the plain `pyproject.toml` the fallback anchors
    at otherwise. `config.anchored`, not `config.source`: what disqualifies a rootdir here is
    being derived from the arguments, which would make one argument's meaning depend on the
    others, and a rootdir read off a file on disk is not that whether or not the file claimed
    voci. A run anchored by neither falls back to the enclosing `.git` directory instead, when
    there is one -- a weaker anchor than a `pyproject.toml` (see `_config.resolve`), but still a
    fixed point, unlike `config.rootdir` in that case, which is just the search start and would
    make one argument's meaning depend on the others were it used here. With none of the three,
    there's no anchor to re-read against and the arguments are left alone. Searched at all only
    when some argument names nothing from here, the uncommon case.
    """
    if all(target.path.is_absolute() or target.path.exists() for target in targets):
        return targets
    config = _config.resolve([])
    rootdir = config.rootdir if config.anchored else config.git_root
    if rootdir is None:
        return targets
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
    obviously dangerous but also doesn't look like a previous voci basetemp is refused instead
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
    into `voci` as arguments) without stripping anything. An id's path is relative to the
    rootdir, and so is an argument that names nothing from the current directory, so a run
    with a fixed rootdir -- one a `[tool.voci]` table gives, or failing that the enclosing
    `.git` directory -- takes its own ids back from any directory under it. Skips and
    collection errors are shown the way a real run shows them: `--collect-only` is how a
    suite is inspected before it runs, and a file that failed to import is exactly what
    such an inspection is looking for.
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

    return _collection_exit_status(ids=ids, skipped=skipped, errors=errors)


def _collection_exit_status(
    *, ids: Sequence[str], skipped: Sequence[object], errors: Sequence[object]
) -> int:
    """The exit code for a run that stopped at collection -- shared by `--collect-only`'s
    plain-text report and `--co-json`'s JSON one.

    Spelled out rather than deferred to `_run.exit_code_for`, which reads an empty result
    list as "nothing ran, so nothing was collected" -- true of a real run, false here,
    where nothing running is the whole point.
    """
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
    """CLI > `[tool.voci]` > built-in default, spelled out once so every merged
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
    established. Called once per run that actually collected, and only when `narrowed_by_selection`
    is false -- `found` rather than whatever `--lf`/`--ff` narrowed or reordered it into, matching
    `_cache.merge`'s own use of it below: the index describes what a file holds, not what one
    particular invocation asked to see. -k/-m/an id argument narrow `found` itself, at the
    `collect()` call that built it, which is what callers check before ever reaching here.

    Re-read at the write like `_cache.update`, and for the same reason: two runs sharing a rootdir
    both write a whole payload, so merging into the snapshot taken at startup lets the second to
    finish drop the entries the first added. It costs a later `--collect-only` its shortcut rather
    than anything correct -- `_index.answer` re-stats every entry and falls back to a real
    collection on any mismatch -- but the entries are just as cheap to keep.
    """
    _index.save(
        rootdir,
        _index.refresh(
            {**previous, **_index.load(rootdir)},
            found,
            rootdir=rootdir,
            files=files,
            discovered=discovered,
            roots=roots,
        ),
    )


def _report_warnings(rootdir: Path, *, stream: TextIO = sys.stdout) -> None:
    """Print the warnings a run raised on its way to an exit that never reaches `Reporter`.

    `stream` defaults to stdout, same as every other report this early-exit path prints, but
    `--co-json` passes stderr instead: that mode's contract is one line of JSON on stdout and
    nothing else, so anything a diagnostic like this has to say goes where it won't be mistaken
    for part of the payload.
    """
    _report.print_warnings(
        [(_report.NO_TEST_RUNNING, _warnings.session_warnings())],
        stream=stream,
        rootdir=rootdir,
        color_enabled=_color.color_enabled(stream),
    )


def _parse_filters(
    config: _config.Config, from_cli: Sequence[str]
) -> tuple[str | None, tuple[_warnings.WarningFilter, ...]]:
    """The run's warning filters, `[tool.voci]`'s first and the `-W` ones after — or a usage
    error naming whichever of the two wrote the spec that couldn't be parsed."""
    configured = config.filterwarnings or ()
    try:
        return None, _warnings.parse_filters((*configured, *from_cli))
    except _warnings.FilterError as exc:
        source = f"{config.source} 'filterwarnings':" if exc.spec in configured else "-W"
        return f"{source} {exc}", ()


@dataclass(frozen=True)
class _PreparedRun:
    """Everything `main`'s execute phase needs, once every usage error checkable before a
    single test file is touched has been ruled out by `_prepare_run`: the compiled
    `-k`/`-m` expressions (and whether either of them, or an id selector, narrows this
    run), the resolved `[tool.voci]` config, the three-tier-resolved run options, and
    the roots collection walks."""

    id_selection: _targets.IdSelection | None
    markexpr: _selection.TagExpression | None
    keywordexpr: _selection.KeywordExpression | None
    narrowed_by_selection: bool
    config: _config.Config
    concurrency: int
    timeout: float | None
    watchdog: float
    filterwarnings: tuple[str, ...]
    roots: list[Path]
    maxfail: int | None


def _check_flag_contradictions(args: argparse.Namespace) -> str | None:
    """The first mutually-exclusive-flags usage error, checkable from `args` alone --
    cheap, so `_prepare_run` checks these before anything else.

    argparse's choices= can't express "unless this other flag is set", so
    --rewrite-cache/--assert=plain and --serial/--concurrency are checked by hand here
    instead. --basetemp is the one "does real work" flag whose failure mode is
    irreversible (_capture.install eventually shutil.rmtrees it), so it's checked here
    too, before collection and rewrite-hook setup run for a call that was always going
    to fail. Each of these is worth reporting rather than silently picking a winner --
    a run that quietly ignored --serial (or -x) would report the wrong thing about what
    it did.
    """
    if args.assert_mode == "plain" and args.rewrite_cache is not None:
        return (
            "--rewrite-cache has no effect with --assert=plain "
            "(plain mode never touches the rewrite cache)"
        )
    basetemp_problem = _invalid_basetemp_argument(args.basetemp)
    if basetemp_problem is not None:
        return basetemp_problem
    if args.serial and args.concurrency is not None and args.concurrency != 1:
        return f"--serial is --concurrency=1, and --concurrency={args.concurrency} was also given"
    return None


def _resolve_limits(args: argparse.Namespace) -> tuple[str | None, int | None]:
    """`-x`/`--maxfail`/`--ff`/`--lf`/`--durations`'s own contradictions and range
    checks, and the resolved `maxfail` once none of them fired -- checked by hand
    rather than through argparse, same as --concurrency and --timeout, for exit code 4
    with a voci-styled message instead of argparse's generic one.
    """
    # One says "run only these", the other "run everything, these first"; picking a
    # winner silently would misreport what the run was.
    if args.last_failed and args.failed_first:
        return (
            "--lf runs only the last run's failures and --ff runs the whole suite with "
            "them first -- pass one or the other",
            None,
        )
    if args.exitfirst and args.maxfail is not None and args.maxfail != 1:
        return f"-x is --maxfail=1, and --maxfail={args.maxfail} was also given", None
    maxfail = 1 if args.exitfirst else args.maxfail
    if maxfail is not None and maxfail < 1:
        return f"--maxfail must be a positive integer, got {maxfail}", None
    if args.durations < 0:
        return f"--durations must be zero or more, got {args.durations}", None
    return None, maxfail


def _resolve_targets(
    args: argparse.Namespace,
) -> tuple[str | None, list[_targets.Target], _targets.IdSelection | None]:
    """PATHS parsed and validated, plus the id-selection an explicit `::` selector
    carries (`None` if none did) -- or the first usage error found doing so.

    Test ids are rootdir-relative wherever voci prints them, so an argument naming
    nothing from the current directory gets that reading before anything else looks at
    it: pasting an id `--collect-only` printed back as an argument is the point. A
    typo'd path and a genuinely empty suite must not look the same afterwards: without
    the validation below, a bad path would silently walk to nothing and exit 5 "no
    tests collected", indistinguishable from an honest empty selection.
    """
    targets = [_targets.parse_target(raw) for raw in args.paths]
    try:
        targets = _reread_on_rootdir(targets)
    except _config.ConfigError as exc:
        return str(exc), [], None
    problem = _invalid_target_argument(targets)
    if problem is not None:
        return problem, [], None
    # None unless some argument actually carried a `::` selector, which is what lets
    # collection skip id filtering entirely in the common case.
    return None, targets, _targets.IdSelection.of(targets)


def _compile_selectors(
    args: argparse.Namespace,
) -> tuple[str | None, _selection.TagExpression | None, _selection.KeywordExpression | None]:
    """The compiled `-k`/`-m` expressions, or the first malformed one -- compiled up
    front, before collection does any real work, so it fails fast with a usage error
    rather than surfacing mid-collection."""
    markexpr = None
    keywordexpr = None
    try:
        if args.markexpr is not None:
            markexpr = _selection.compile_tag_expression(args.markexpr)
        if args.keywordexpr is not None:
            keywordexpr = _selection.compile_keyword_expression(args.keywordexpr)
    except _selection.SelectionError as exc:
        return str(exc), None, None
    return None, markexpr, keywordexpr


@dataclass(frozen=True)
class _ResolvedOptions:
    """The three-tier-resolved concurrency/timeout/loop-watchdog `_resolve_options`
    settled on, once none of them turned out to be out of range."""

    concurrency: int
    timeout: float | None
    watchdog: float


def _option_source(cli_value: object, cli_name: str, config: _config.Config, key: str) -> str:
    """Named by its actual source (the CLI flag if given, else the config file that set
    it) rather than always saying the flag's own name -- a bad `[tool.voci]` value
    shouldn't point the user at a flag they never touched."""
    return cli_name if cli_value is not None else f"{config.source} {key!r}"


def _resolve_options(
    args: argparse.Namespace, config: _config.Config
) -> tuple[str | None, _ResolvedOptions]:
    """CLI > `[tool.voci]` > built-in default for concurrency/timeout/loop-watchdog, via
    `_resolve_layered` -- `--serial` joins the CLI tier, same rank as `--concurrency`
    itself, whichever of the two spellings was used -- and the first one out of range.
    The `_ResolvedOptions` alongside a usage error is meaningless and must not be read.
    """
    effective_concurrency = _resolve_layered(
        1 if args.serial else args.concurrency, config.concurrency, _run.DEFAULT_CONCURRENCY
    )
    effective_timeout = _resolve_layered(args.timeout, config.timeout)
    effective_watchdog = _resolve_layered(
        args.loop_watchdog, config.loop_watchdog, _safety.DEFAULT_LOOP_WATCHDOG
    )
    unset = _ResolvedOptions(0, None, 0)
    if effective_concurrency < 1:
        source = _option_source(args.concurrency, "--concurrency", config, "concurrency")
        return f"{source} must be a positive integer, got {effective_concurrency}", unset
    if effective_timeout is not None and not (
        math.isfinite(effective_timeout) and effective_timeout > 0
    ):
        source = _option_source(args.timeout, "--timeout", config, "timeout")
        return (
            f"{source} must be a positive, finite number of seconds, got {effective_timeout}",
            unset,
        )
    # 0 is a real value here (the diagnostic off) rather than a rejected one, so only
    # negative and non-finite values are usage errors.
    if not math.isfinite(effective_watchdog) or effective_watchdog < 0:
        source = _option_source(args.loop_watchdog, "--loop-watchdog", config, "loop_watchdog")
        return (
            f"{source} must be zero (off) or a positive, finite number of seconds, got "
            f"{effective_watchdog}",
            unset,
        )
    return None, _ResolvedOptions(effective_concurrency, effective_timeout, effective_watchdog)


def _resolve_roots(
    targets: list[_targets.Target], config: _config.Config
) -> tuple[str | None, list[Path]]:
    """PATHS > configured testpaths > the rootdir, or the first invalid testpaths entry
    (the `list[Path]` alongside it is then empty and must not be read). config.testpaths
    entries are written relative to wherever [tool.voci] was declared, so they're
    resolved against config.rootdir here, not cwd().

    is not None, not truthiness: testpaths = [] is a real, if unusual, thing to write
    and means "nothing" -- truthiness would silently run the built-in default instead.
    """
    if targets:
        return None, [target.path for target in targets]
    if config.testpaths is not None:
        roots = [config.rootdir / p for p in config.testpaths]
        # Mirrors _invalid_target_argument's reasoning for CLI paths: a typo'd testpaths
        # entry must not silently look like an honest empty selection either.
        for root, raw in zip(roots, config.testpaths, strict=True):
            if not root.exists():
                return f"{config.source}: testpaths entry does not exist: {raw!r}", []
        return None, roots
    # config.source, not config.anchored: a configured project keeps resolving this tier
    # against its rootdir exactly as it always has -- a table that sets no testpaths is a
    # thin thing to read intent from, but changing what it selects is not this change's
    # business. Everything else resolves against cwd, so the rootdir climbing to the nearest
    # pyproject.toml (`_config.resolve`) doesn't quietly widen a bare `voci` run inside
    # tests/unit into the whole suite the new rootdir can see.
    return None, _default_test_roots(config.rootdir if config.source is not None else None)


def _prepare_run(args: argparse.Namespace) -> tuple[str | None, _PreparedRun | None]:
    """Resolve `args` into everything `main`'s execute phase needs, or the first usage
    error found along the way -- unprefixed, like `_parse_filters`'s, since `main` is
    what knows to prefix every one of these the same way.

    Deliberately linear, in the same order each helper below is called: a flag's own
    contradictions first (cheap, and needs nothing but `args`), then targets, then the
    -k/-m expressions the id-selection needs to make sense of, then config (which
    targets anchor the search for), then the options config and CLI flags both feed,
    then the roots collection actually walks -- each tier depends on the ones before
    it, so this is not a set of independent checks that could run in a different order
    or in parallel.
    """
    problem = _check_flag_contradictions(args)
    if problem is not None:
        return problem, None
    problem, maxfail = _resolve_limits(args)
    if problem is not None:
        return problem, None

    problem, targets, id_selection = _resolve_targets(args)
    if problem is not None:
        return problem, None

    problem, markexpr, keywordexpr = _compile_selectors(args)
    if problem is not None:
        return problem, None
    # -k/-m/an id argument bake their narrowing straight into collect()'s own records and
    # deselected -- `tag_expr`/`keyword_expr`/`id_selection` below -- so a run any of them
    # narrows sees a partial file, never the whole of what it holds. Read in two places: the
    # collection-index fast path won't answer under one (it has no notion of the narrowing to
    # replay), and a real collection under one must not write its partial view of a file into
    # the index either, where a later un-narrowed --collect-only would take it as the whole
    # file. --lf/--ff don't have this problem -- they narrow which files collect() sees, never
    # what one collected file reports -- so they're not part of this.
    narrowed_by_selection = (
        markexpr is not None or keywordexpr is not None or id_selection is not None
    )

    # [tool.voci]-anchored upward search, stopping at the git root -- see
    # _config.resolve's own docstring for exactly where it starts and stops. A
    # malformed pyproject.toml/[tool.voci] table is always a usage error: never
    # silently fall back to defaults over a config the user wrote but voci can't honor.
    # The file part of a `path.py::test_name` argument is what anchors the search, the
    # same as a plain path does.
    try:
        config = _config.resolve([target.path for target in targets])
    except _config.ConfigError as exc:
        return str(exc), None

    problem, options = _resolve_options(args, config)
    if problem is not None:
        return problem, None
    # Both tiers, in precedence order rather than layered like the scalars above: warning
    # filters accumulate, and the last one to match a warning is the one that decides it,
    # so a `-W` on the command line simply follows what [tool.voci] already said. Parsed
    # further down, once rootdir is on sys.path: a spec names a warning category, which for a
    # class the suite defines itself is not importable before then.
    effective_filterwarnings = (*(config.filterwarnings or ()), *args.filterwarnings)

    problem, roots = _resolve_roots(targets, config)
    if problem is not None:
        return problem, None

    return None, _PreparedRun(
        id_selection=id_selection,
        markexpr=markexpr,
        keywordexpr=keywordexpr,
        narrowed_by_selection=narrowed_by_selection,
        config=config,
        concurrency=options.concurrency,
        timeout=options.timeout,
        watchdog=options.watchdog,
        filterwarnings=effective_filterwarnings,
        roots=roots,
        maxfail=maxfail,
    )


def _settle(
    results: Sequence[_run.TestResult],
    collected: _collect.CollectionResult,
    found: _collect.CollectionResult,
    *,
    files: Sequence[Path],
    discovered: Sequence[Path],
    last_run: _cache.LastRun,
    roots: Sequence[Path],
    rootdir: Path,
) -> None:
    """Tell the cache what this run is entitled to, `last_run` updated with it and written back.

    `collected` is what this run actually ran (`--lf`/`--ff` narrowed or reordered, if either
    applied); `found` is collection's own unnarrowed result, which is what `_cache.update` and the
    collection index both key off -- the index describes what a file holds, not what one
    particular invocation asked to see. `files`/`discovered` are the same two collection was
    handed (see `main`'s own comments for why they can differ under `--lf`).

    One computation is used both ways round: what this run can settle is exactly what it may
    record, so no error goes into the cache that no later run could take back out.

    `_cache.update`, not `_cache.save(rootdir, _cache.merge(...))`: `last_run` is the baseline
    loaded at startup, and merging into it here would leave the window for a lost update open for
    the entire length of the run -- long enough that a scoped run finishing after a full one drops
    the full run's new failures. `update` re-reads the freshest cache on disk at the write instead,
    falling back to `last_run` only if that re-read fails. See `plans/rationale/cache.md`.
    """
    resolved_rootdir = rootdir.resolve()
    attempted = {str(_collect.display_path(path, resolved_rootdir)) for path in files}
    answered = _lastfailed.settled_paths(attempted, rootdir=resolved_rootdir)
    errored = _lastfailed.error_paths(collected.errors, answered=answered)
    # Recorded paths this run establishes nothing will ever collect again: gone from disk, or in
    # a directory it walked and no longer discovered there. Nothing else is in a position to take
    # these out of the cache. Skipped outright with nothing recorded: there is no entry for a
    # walk to declare dead, and resolving every discovered path to find that out is not free.
    gone: set[str] = set()
    if not last_run.is_empty():
        gone = _lastfailed.dead_paths(
            last_run,
            discovered={str(_collect.display_path(path, resolved_rootdir)) for path in discovered},
            roots=roots,
            rootdir=resolved_rootdir,
        )
    # A file that collected tests was read, whatever else in it went wrong: one malformed test
    # does not make the ids beside it unknowable, and treating the file as unread would leave a
    # renamed sibling recorded for good.
    produced = {str(record.path) for record in found.records}
    produced |= {str(skip.path) for skip in found.skipped}
    # A CANCELLED test never got to say anything about the code under test, so it settles
    # nothing: without this, the very stop --lf exists to iterate through -- `-x`, or a Ctrl-C --
    # would drop every failure it cut short. `vanished` is the other direction: a recorded id a
    # file collection fully read no longer has is settled by its absence, there being no run left
    # to settle it.
    settled_ids = {r.id for r in results if r.outcome is not _run.Outcome.CANCELLED}
    settled_ids |= {s.id for s in collected.skipped}
    settled_ids |= _lastfailed.vanished(
        last_run,
        found,
        known_files=_lastfailed.read_files(attempted, errored - produced) | gone,
    )
    _cache.update(
        rootdir,
        last_run,
        failed=[r.id for r in results if r.outcome in _run.FAILING_OUTCOMES],
        errored=errored,
        settled_ids=settled_ids,
        settled_files=answered | gone,
    )


@contextlib.contextmanager
def _installed_session(
    config: _config.Config,
    roots: Sequence[Path],
    setup: _rewrite.AssertionSetup,
    filterwarnings: Sequence[str],
) -> Iterator[str | None]:
    """Install everything a run's execute phase needs on process-global state -- `config.env`,
    `rootdir` on `sys.path`, the assertion-rewrite import hook, and the parsed `-W`/
    `[tool.voci]` warning filters, in that order -- and tear all four back down again on the way
    out, however the `with` block exits: a return, an uncaught exception, or a `KeyboardInterrupt`
    alike. Yields the first usage error found while resolving the filters, or `None` once every
    one of the four is live; the caller is expected to bail out on the former before doing
    anything the latter three make possible.

    Order matters past the first two: resolving a filter's category can import the module holding
    it, which must go through the rewrite hook like any other test-adjacent import, and a module
    that warns at import time is only caught once the warnings shim is live too -- so filters
    resolve after the hook and before the shim, not before either.

    Each of the four remembers whether *this* call is the one that installed it -- `_rewrite`'s
    and `_warnings`' own `install` are both documented no-ops when an enclosing call already did,
    and env/`sys.path` are checked the same way by hand -- so a nested `main()` call (this
    package's own test suite calls it repeatedly in-process, and an embedding caller may too)
    tears down only what it put there, leaving an outer call's own setup alone.
    """
    # Not monkeypatch -- this is production code. Restored to exactly what it was before this
    # call touched it, key by key, so one main() call's config never leaks into the next.
    env_backup = {key: os.environ.get(key) for key in config.env}
    os.environ.update(config.env)

    # rootdir goes on sys.path exactly once, before the first test module import, so a plain
    # absolute import rooted at rootdir resolves via ordinary PEP 420 namespace-package lookup --
    # no __init__.py required. Collection's own import mechanism is untouched by this, so
    # relative imports between test modules (`from .conftest import x`) stay unsupported.
    # sys.path[0], not appended: matches pytest's prepend import-mode convention, so the test
    # tree's own sources shadow a same-named installed package. str(...), not the Path: sys.path
    # holds strings, and comparing a Path against it with `in` would never match, inserting a
    # fresh duplicate on every main() call.
    rootdir_str = str(config.rootdir)
    sys_path_inserted = rootdir_str not in sys.path
    if sys_path_inserted:
        sys.path.insert(0, rootdir_str)

    # Set here, not just inside the try below: if _rewrite.installed_hook() itself raised, the
    # finally's `if not hook_already_installed:` would otherwise hit an unbound name. False is
    # also the safer fallback value -- it makes finally attempt an uninstall(), not skip one.
    hook_already_installed = False
    warnings_installed = False
    try:
        # Must be installed before any test module is imported below -- a module already in
        # sys.modules can't retroactively be rewritten.
        #
        # Known cost, not fixed here: install walks every .py under roots for its own file list,
        # and discover_files (in the caller, after this yields) walks the same roots again for
        # test files specifically -- two full traversals per run. They want different filters
        # (all .py vs test_*.py/*_test.py), so unifying them means changing install's signature
        # to accept a pre-discovered file list.
        hook_already_installed = _rewrite.installed_hook() is not None
        _rewrite.install(roots, setup=setup, warn=False)
        problem, session_filters = _parse_filters(config, filterwarnings)
        if problem is None:
            warnings_installed = _warnings.install(session_filters)
        yield problem
    finally:
        # Only torn down if this call is the one that put it there: install is a documented
        # no-op when a hook is already on sys.meta_path, so an embedder (or a nested main())
        # that installed its own hook first must keep it.
        if not hook_already_installed:
            _rewrite.uninstall()
        # Same rule, and the same reason: `warnings.showwarning` is process-global, so a nested
        # main() leaves the outer call's shim in place for the outer call to remove.
        if warnings_installed:
            _warnings.uninstall()
        # Symmetric with hook_already_installed above: only remove what this call put on
        # sys.path, and only if it's still there.
        if sys_path_inserted:
            with contextlib.suppress(ValueError):
                sys.path.remove(rootdir_str)
        # Whatever this run did or didn't get to, the rewriter may have written bytecode into
        # the cache directory on its way there.
        _cache.ensure_gitignore(config.rootdir)
        # Symmetric with env_backup's own comment above: restores exactly the keys this call
        # touched, to exactly what they were before it touched them.
        for key, prev_value in env_backup.items():
            if prev_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev_value


@dataclass
class _RunSession:
    """State the execute phase (everything `_installed_session` makes possible) threads
    from one sub-phase to the next, one object passed around rather than the same
    fields repeated at every call site -- built once `_prepare_run` and the header
    print are done."""

    args: argparse.Namespace
    prepared: _PreparedRun
    setup: _rewrite.AssertionSetup
    last_run: _cache.LastRun
    collection_index: _index.Index
    wall_start: float

    @property
    def rootdir(self) -> Path:
        return self.prepared.config.rootdir

    @property
    def verbosity(self) -> int:
        return (1 if self.args.verbose else 0) - (1 if self.args.quiet else 0)

    @property
    def replay_last_failed(self) -> bool:
        # An empty cache leaves both flags meaning "the whole suite, in logical order",
        # which is what makes --lf safe to leave in a shell alias -- a first run, or one
        # that went green, runs everything rather than nothing.
        return self.args.last_failed and not self.last_run.is_empty()

    @property
    def replay_failed_first(self) -> bool:
        return self.args.failed_first and not self.last_run.is_empty()


def _print_run_header(session: _RunSession) -> None:
    """The two header lines a run at non-negative verbosity prints before it does
    anything else: the assertion-rewrite decision and which config (if any) it read --
    both transparency, so neither is silently inferred from behavior alone. Skipped
    entirely by -q and --co-json: -q because this describes how the run was set up,
    which is exactly the part a quiet run is asking to do without; --co-json because its
    contract is one line of JSON on stdout, nothing else, regardless of verbosity.
    """
    args = session.args
    if session.verbosity < 0 or args.co_json:
        return
    header = session.setup.header_line()
    if header is not None:
        print(header)
    # Same transparency plan's own header line gives the assertion-rewrite decision --
    # a run silently picking up config the user forgot was there is exactly the kind of
    # surprise this avoids. _friendly_path: this is usually a couple of directories
    # under cwd (or cwd itself), and the absolute form is just noise at that distance.
    config_source = session.prepared.config.source
    if config_source is not None:
        print(f"config: {_friendly_path(config_source)}")
    else:
        print("config: none")
    # Said out loud rather than left to be inferred from the test count: the difference
    # between "--lf ran four tests" and "--lf ran the suite" is the whole point of asking.
    if (args.last_failed or args.failed_first) and session.last_run.is_empty():
        flag = "--lf" if args.last_failed else "--ff"
        print(f"{flag}: nothing recorded -- running the whole suite")


def _discover(session: _RunSession) -> tuple[list[Path], list[Path]]:
    """Files discovery finds under `[tool.voci]`'s (or the built-in) patterns/ignores --
    `discovered`, the unnarrowed set collection is handed unless `--lf` narrows `files`
    further downstream. config.test_file_patterns/config.ignore replace discover_files's
    own defaults outright when set, not add to them -- a user who wants "the defaults
    plus one more" repeats the defaults themselves. is not None, not truthiness, for
    both: an explicit [] is a real, if unusual, thing to write and means "none".
    """
    config = session.prepared.config
    patterns = (
        config.test_file_patterns
        if config.test_file_patterns is not None
        else _discovery.DEFAULT_TEST_FILE_PATTERNS
    )
    ignore_dirs = (
        frozenset(config.ignore) if config.ignore is not None else _discovery.DEFAULT_IGNORE_DIRS
    )
    files = _discovery.discover_files(
        session.prepared.roots, patterns=patterns, ignore_dirs=ignore_dirs
    )
    # The same list, deliberately: `discovered` is never rebound after this, while a
    # caller under `--lf` rebinds its own `files` to a narrowed copy -- see
    # `_collect_and_narrow`.
    return files, files


def _try_fast_collect_only(session: _RunSession, files: Sequence[Path]) -> int | None:
    """The collect-only fast path: a plain `--collect-only`/`--co-json` -- no -k/-m/id/
    --lf/--ff narrowing it to something the index doesn't track -- can answer straight
    from `collection_index` when every discovered file is still fresh in it, skipping
    `collect()` and therefore every import it would have done. Returns the exit status
    when it answered, else `None` to fall through to a real collection exactly as
    before."""
    args = session.args
    if not (
        (args.collect_only or args.co_json)
        and not session.replay_last_failed
        and not session.replay_failed_first
        and not session.prepared.narrowed_by_selection
    ):
        return None
    fast_answer = _index.answer(session.collection_index, files, rootdir=session.rootdir)
    if fast_answer is None:
        return None
    warnings_stream = sys.stderr if args.co_json else sys.stdout
    if args.co_json:
        _collect_json.print_report(
            tests=[
                _collect_json.TestLocation(id=id_, path=path, lineno=lineno)
                for id_, path, lineno in zip(
                    fast_answer.ids, fast_answer.paths, fast_answer.lines, strict=True
                )
            ],
            skipped=[
                (id_, path, reason)
                for (id_, reason), path in zip(
                    fast_answer.skipped, fast_answer.skipped_paths, strict=True
                )
            ],
            deselected=(),
            errors=(),
            rootdir=session.rootdir,
        )
        status = _collection_exit_status(
            ids=fast_answer.ids, skipped=fast_answer.skipped, errors=()
        )
    else:
        status = _report_collection(
            ids=fast_answer.ids,
            skipped=fast_answer.skipped,
            deselected=0,
            errors=(),
            color_enabled=_color.color_enabled(sys.stdout),
        )
    # Called for symmetry with every other exit through this function: this run
    # imported nothing, so in the ordinary case there is nothing recorded to print.
    _report_warnings(session.rootdir, stream=warnings_stream)
    return status


def _unmatched_id_status(session: _RunSession, collected: _collect.CollectionResult) -> int | None:
    """4, having printed which id(s) matched nothing, if `id_selection` named one and
    this run's collection left any unmatched -- else `None` to keep going.

    Same reasoning as a path that doesn't exist, one level down: a mistyped test id
    would otherwise select nothing and exit 5, indistinguishable from a file that
    genuinely holds no tests.

    Every id collection produced counts as a match, not just the selected ones: a
    `@voci.skip`-marked test is a normal thing to name, and an id that `-k`/`-m` then
    deselects is an empty intersection the user asked for, not a typo. Skipped entirely
    when a file failed to import, since the ids it would have contributed are
    unknowable -- the traceback printed by `_report_run` is the real story there.

    collected.unexpanded is the part of that which stops short of its own `[case]` ids
    (a skip, or a test -m excluded), so `test_role[admin]` naming a case of one of those
    counts as a match rather than reading as a typo.

    Not under --lf: an id naming a test that passed last time matches nothing there by
    design, and voci has no way to tell that apart from a typo.
    """
    id_selection = session.prepared.id_selection
    if id_selection is None or collected.errors or session.replay_last_failed:
        return None
    missing = id_selection.unmatched(
        [record.id for record in collected.records]
        + [skipped.id for skipped in collected.skipped]
        + collected.deselected,
        unexpanded=collected.unexpanded,
        rootdir=session.rootdir,
    )
    if not missing:
        return None
    print(f"voci: no test matches {', '.join(repr(name) for name in missing)}", file=sys.stderr)
    _report_warnings(session.rootdir, stream=sys.stderr if session.args.co_json else sys.stdout)
    return 4


def _collect_and_narrow(
    session: _RunSession, files: list[Path], discovered: Sequence[Path]
) -> tuple[_collect.CollectionResult, _collect.CollectionResult, list[Path]]:
    """Collection, then `--lf`/`--ff` narrowing on top of `-k`/`-m`/id-selection, applied
    after rather than instead of them: `--lf` narrows a selection the other flags
    already made, so `voci --lf -k users` means both. Returns `found` -- collection's
    own unnarrowed result, kept for the cache write at the end -- `collected` -- what
    this run actually runs, before `_unmatched_id_status` has had a say -- and `files`,
    `--lf`-narrowed if that applied.
    """
    rootdir = session.rootdir
    if session.replay_last_failed:
        files = _lastfailed.candidate_files(files, session.last_run, rootdir=rootdir)
    collected = _collect.collect(
        files,
        rootdir=rootdir,
        tag_expr=session.prepared.markexpr,
        keyword_expr=session.prepared.keywordexpr,
        id_selection=session.prepared.id_selection,
        # The unnarrowed set, so --lf leaving a test module out doesn't turn its
        # `voci.use(...)` into a misplaced declaration. Nothing here is imported. Only
        # when narrowed: otherwise it is `files`, which the loop below walks anyway.
        collectible=discovered if session.replay_last_failed else (),
    )
    # What collection itself found is kept for the cache write at the end: `select` moves
    # the tests --lf wasn't asked for into `deselected`, and `vanished` reads a deselected
    # unexpanded test as "this run never built its cases, so its recorded ones stand" --
    # true of a -k/-m deselection, and false of --lf's own, whose skips it would otherwise
    # strand in the cache for good.
    found = collected
    if session.replay_last_failed:
        collected = _lastfailed.select(collected, session.last_run)
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
    elif session.replay_failed_first:
        collected = _lastfailed.reorder(collected, session.last_run)

    return found, collected, files


def _maybe_save_collection_index(
    session: _RunSession,
    found: _collect.CollectionResult,
    *,
    files: Sequence[Path],
    discovered: Sequence[Path],
) -> None:
    """Persist the collection index updated with what this run's real collection
    established, unless something narrowed the request past what the index tracks --
    called once from each of `_execute`'s two terminal branches."""
    if session.prepared.narrowed_by_selection:
        return
    _save_collection_index(
        session.rootdir,
        session.collection_index,
        found,
        files=files,
        discovered=discovered,
        roots=session.prepared.roots,
    )


def _finish_collect_only(
    session: _RunSession,
    found: _collect.CollectionResult,
    collected: _collect.CollectionResult,
    *,
    files: Sequence[Path],
    discovered: Sequence[Path],
) -> int:
    """Report and exit for `--collect-only`/`--co-json`: saves the collection index when
    nothing narrowed the request, prints what collection found (or would have run), and
    returns the exit status -- this run never reaches `run_suite`."""
    args = session.args
    rootdir = session.rootdir
    _maybe_save_collection_index(session, found, files=files, discovered=discovered)
    if args.co_json:
        _collect_json.print_report(
            tests=[
                _collect_json.TestLocation(
                    id=record.id, path=str(record.path), lineno=record.lineno
                )
                for record in collected.records
            ],
            skipped=[(skip.id, str(skip.path), skip.reason) for skip in collected.skipped],
            deselected=collected.deselected,
            errors=[(str(error.path), error.message) for error in collected.errors],
            rootdir=rootdir,
        )
        status = _collection_exit_status(
            ids=[record.id for record in collected.records],
            skipped=collected.skipped,
            errors=collected.errors,
        )
    else:
        # Shared with reporter's own coloring (it resolves the same thing internally
        # for its own prints) so this matches _report_run's file blocks instead of
        # being colored by a different rule.
        status = _report_collection(
            ids=[record.id for record in collected.records],
            skipped=[(skip.id, skip.reason) for skip in collected.skipped],
            deselected=len(collected.deselected),
            errors=collected.errors,
            color_enabled=_color.color_enabled(sys.stdout),
        )
    # Collection is what imports every test module, so a module that warns at import
    # has warned by now -- and this is the only report this run will print.
    _report_warnings(rootdir, stream=sys.stderr if args.co_json else sys.stdout)
    return status


@dataclass(frozen=True)
class _Execution:
    """What `run_suite` produced, and what `_execute_suite` built alongside it -- everything
    `_report_run` and the cache write after it need."""

    results: list[_run.TestResult]
    reporter: _report.Reporter
    interrupted: bool
    unattributed: list[str]
    wall_clock: float


def _execute_suite(session: _RunSession, collected: _collect.CollectionResult) -> _Execution:
    """Builds the reporter and runs `run_suite`, returning what everything after it needs."""
    args = session.args
    prepared = session.prepared
    capture_passthrough = args.capture == "no" or args.capture_s
    # Populated by run_suite iff non-None -- see _builtins/capture.py's module docstring
    # for what can land here. Empty in the common case. Rendered by reporter.finish
    # below, not printed here directly, keeping every "what got printed and in what
    # order" decision in one place.
    unattributed: list[str] = []

    # Reporter groups TestResults (which carry no path of their own) back into per-file
    # blocks by walking collected.records itself -- handed to it directly, not reduced
    # to an {id: path} dict first, since a dict comprehension keyed by id would silently
    # collapse two records sharing an id (an ordinary case: a factory-generated test
    # repeats its id for every instance it produces). See Reporter's own docstring for
    # the full reasoning.
    reporter = _report.Reporter(
        records=collected.records,
        skipped=collected.skipped,
        capture_passthrough=capture_passthrough,
        stream=sys.stdout,
        verbosity=session.verbosity,
        durations=args.durations,
        rootdir=session.rootdir,
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
        concurrency=prepared.concurrency,
        timeout=prepared.timeout,
        capture_passthrough=capture_passthrough,
        maxfail=prepared.maxfail,
        basetemp=args.basetemp,
        unattributed_output=unattributed,
        on_result=reporter.on_result,
        on_interrupt=on_interrupt,
        # 0 means "off" at the CLI; run_suite spells that None, and treats a
        # non-positive number the same way regardless.
        loop_watchdog=prepared.watchdog or None,
        filterwarnings=prepared.filterwarnings,
        # setup.mode/setup.cache_dir, not args.assert_mode/args.rewrite_cache: an
        # isolated test's subprocess must reproduce what this run actually decided
        # (a --assert=rewrite request can still fall back to plain), not re-derive
        # and re-warn about it once per isolated test.
        isolated=_isolated.IsolatedConfig(
            rootdir=session.rootdir,
            assert_mode=session.setup.mode,
            assert_cache_dir=session.setup.cache_dir,
            rewrite_roots=tuple(prepared.roots),
        ),
    )
    wall_clock = time.monotonic() - session.wall_start
    return _Execution(results, reporter, interrupted, unattributed, wall_clock)


def _report_run(
    session: _RunSession,
    collected: _collect.CollectionResult,
    execution: _Execution,
    *,
    color_enabled: bool,
) -> int:
    """Everything after `run_suite` returns: the stop reason (if any), collection-error
    blocks, reporter's own totals, and the exit code."""
    # Before this function prints anything of its own: a run stopped by --maxfail
    # leaves a file block unprinted, and -q leaves its line of characters unclosed.
    execution.reporter.flush_pending()

    error_word = _color.paint("COLLECTION ERROR", _color.RED, enabled=color_enabled)
    for error in collected.errors:
        print(f"{error.path} {error_word}")
        print(error.message)

    # run_suite returns one result per test that ran -- a cancelled test included -- so
    # anything collection handed it that isn't in there is a test the run stopped before
    # it ever started.
    not_run = len(collected.records) - len(execution.results)
    stopped_by = "interrupted" if execution.interrupted else "--maxfail"
    if execution.interrupted:
        print(_color.paint("INTERRUPTED (Ctrl-C)", _color.YELLOW, enabled=color_enabled))
    elif not_run:
        print(
            _color.paint(
                f"stopped after {session.prepared.maxfail} failed (--maxfail)",
                _color.YELLOW,
                enabled=color_enabled,
            )
        )

    # Every count the run ends on is reporter's to print, so the file blocks above and
    # the totals below can't drift into disagreeing about the same suite. Skips reach it
    # through its constructor, since it counts them per file too.
    execution.reporter.finish(
        execution.results,
        wall_clock=execution.wall_clock,
        unattributed_output=execution.unattributed,
        session_warnings=_warnings.session_warnings(),
        not_run=not_run,
        not_run_label=stopped_by,
        deselected=len(collected.deselected),
        collection_errors=len(collected.errors),
    )

    # 2, not what the partial results happen to add up to: an interrupted run never got
    # to the point of having a verdict, and exiting 0 because the tests that did finish
    # passed would let a Ctrl-C read as success in CI.
    return (
        2
        if execution.interrupted
        else _run.exit_code_for(execution.results, collected.errors, skipped=len(collected.skipped))
    )


def _run_and_report(
    session: _RunSession,
    found: _collect.CollectionResult,
    collected: _collect.CollectionResult,
    *,
    files: Sequence[Path],
    discovered: Sequence[Path],
) -> int:
    """The real run: `run_suite`, then everything after it -- printing results, writing
    `--report-json`, and settling the cache and collection index."""
    rootdir = session.rootdir
    execution = _execute_suite(session, collected)
    exit_status = _report_run(
        session, collected, execution, color_enabled=_color.color_enabled(sys.stdout)
    )
    if session.args.report_json is not None:
        _json_report.write_report(
            session.args.report_json,
            records=collected.records,
            results=execution.results,
            skipped=collected.skipped,
            collection_errors=collected.errors,
            rootdir=rootdir,
            exit_status=exit_status,
            wall_clock=execution.wall_clock,
            session_warnings=_warnings.session_warnings(),
        )
    # Last, after everything this run had to say: a cache voci cannot write costs the
    # next --lf its ordering, and must not touch this one's output or exit code.
    _settle(
        execution.results,
        collected,
        found,
        files=files,
        discovered=discovered,
        last_run=session.last_run,
        roots=session.prepared.roots,
        rootdir=rootdir,
    )
    _maybe_save_collection_index(session, found, files=files, discovered=discovered)
    return exit_status


def _execute(session: _RunSession) -> int:
    """The execute phase itself, everything `_installed_session` makes possible:
    discovery, collection, and either a `--collect-only`/`--co-json` report or a real
    run -- in that order, each depending on what the one before it found."""
    files, discovered = _discover(session)

    fast_status = _try_fast_collect_only(session, files)
    if fast_status is not None:
        return fast_status

    found, collected, files = _collect_and_narrow(session, files, discovered)
    status = _unmatched_id_status(session, collected)
    if status is not None:
        return status

    if session.args.collect_only or session.args.co_json:
        return _finish_collect_only(session, found, collected, files=files, discovered=discovered)
    return _run_and_report(session, found, collected, files=files, discovered=discovered)


def main(argv: list[str] | None = None, *, wall_start: float | None = None) -> int:
    # The process start, not the top of main(): interpreter startup, importing voci,
    # argument parsing, config resolution, discovery, and collection (importing every
    # test module) all happen before a single test runs, and a run that spends its time
    # there rather than executing should say so, not report a "wall" time that only
    # covers the fast part. What remains between this and `time voci` is the launcher
    # in front of the interpreter -- `uv run` and the console script -- which no timer
    # inside the process can see.
    #
    # `wall_start` is a parameter, not always this module's own PROCESS_START, for --watch's
    # sake below: every run after its first is one this same process started well after its own
    # launch, and must time itself from there instead or report an ever-growing "wall" that
    # counts every second spent idling between changes.
    if wall_start is None:
        wall_start = PROCESS_START

    # A concrete list even when the caller passed none, mirroring argparse's own None ->
    # sys.argv[1:] default -- needed below so --watch's own re-invocations of this function
    # have something to filter their own flag out of.
    argv = list(sys.argv[1:] if argv is None else argv)

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.watch:
        # Every re-invocation below is this same function, so args.watch must not still be
        # set on it -- leaving it in would just recurse into this branch forever.
        watch_argv = [item for item in argv if item != "--watch"]
        roots, ignore_dirs = _watch_scope(args)
        return _watch_run(
            base_argv=watch_argv,
            apply_last_failed=not args.failed_first and not args.last_failed,
            roots=roots,
            ignore_dirs=ignore_dirs,
            initial_wall_start=wall_start,
            run_once=main,
        )

    # Every usage error checkable before collection touches a single test file, in one
    # phase: a flag's own contradictions, PATHS, -k/-m, [tool.voci], and the run
    # options all three of those and the CLI feed. See _prepare_run's own docstring for
    # why this has to stay linear.
    problem, prepared = _prepare_run(args)
    if problem is not None:
        print(f"voci: {problem}", file=sys.stderr)
        return 4
    assert prepared is not None

    # Loaded whether or not this run reads it back: the merge at the end of main needs what
    # the previous run recorded about tests this one never reaches.
    last_run = _cache.load(prepared.config.rootdir)
    # Loaded up front for the same reason: a `--collect-only` run below may answer from it
    # without ever reaching `collect()`, and every other run still needs it to build the
    # updated index it writes back at the end.
    collection_index = _index.load(prepared.config.rootdir)

    # Resolved and probed up front so a silent fallback to plain mode is visible
    # before a run commits to it -- a benchmark that silently fell back would be a
    # corrupted one. plan warns on stderr; the header line (when there is one -- see
    # AssertionSetup.header_line) goes to stdout with the report. rootdir, not roots:
    # the cache is a project-wide thing, not tied to whichever subset of it this run
    # happens to be testing.
    setup = _rewrite.plan(
        prepared.roots,
        mode=args.assert_mode,
        cache_dir=args.rewrite_cache,
        rootdir=prepared.config.rootdir,
    )

    session = _RunSession(
        args=args,
        prepared=prepared,
        setup=setup,
        last_run=last_run,
        collection_index=collection_index,
        wall_start=wall_start,
    )
    _print_run_header(session)

    try:
        with _installed_session(
            prepared.config, prepared.roots, setup, args.filterwarnings
        ) as problem:
            if problem is not None:
                print(f"voci: {problem}", file=sys.stderr)
                return 4
            return _execute(session)
    except KeyboardInterrupt:
        # A Ctrl-C run_suite's own handler didn't turn into a graceful stop: the second
        # one (the deliberate "abort now" path), or one that landed while this call was
        # still collecting. Nothing partial is worth printing at that point -- the run
        # was abandoned, not finished.
        print(file=sys.stdout, flush=True)
        print("voci: aborted (Ctrl-C)", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
