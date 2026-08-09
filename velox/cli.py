"""Command-line entrypoint for velox.

M0 (spec/00 §8): discover, import, run, print pass/fail, correct exit code. No `-k`/`-m`/
`--collect-only` selection beyond what's already here — those are later milestones; see
`_discovery`, `_collect`, and `_run` for the pieces this wires together.

M1 concurrency slice (spec/05 §1-4): `--concurrency` and `--timeout` are now real, wired straight
through to `_run.run_suite`, alongside `--assert`/`--rewrite-cache` in the "does real work"
category this docstring already calls out.

M1 capture slice (spec/09): `-s`/`--capture=no` and `--basetemp` join that same category.

M1 reporter slice (spec/10 §2): the flat per-test dump this module used to print itself is gone,
replaced by `_report.Reporter` — jest-style per-file scrollback blocks (flushed as each file
finishes), failure details and the short test summary in logical order, and the wall-vs-Σ
concurrency line. Still not this milestone (spec/00 §7's MVP table, *Deferred* column): the live
footer, `--durations`, JUnit XML, `--report-json`, GH annotations, `--stream-failures`, and any
ANSI/`rich` color — see `_report.py`'s own module docstring for the specifics.

M1 config-loader slice (spec/02 §3): `_config.resolve` finds `[tool.velox]` in `pyproject.toml`
(rootdir search, upward, stopping at the git root) and `main` merges it against the CLI at
`CLI > [tool.velox] > built-in default` for `concurrency`/`timeout`/`testpaths`, applies `env`
before the first test module import, and passes `test_file_patterns`/`ignore` through to
`_discovery.discover_files`. Only the six keys `_config.py`'s own module docstring names — not
`-k`/`-m`/`--seed`/`--serial`/the `VELOX_*` environment tier, all still later milestones.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path
from typing import overload

from velox import __version__, _capture, _collect, _config, _discovery, _report, _rewrite, _run


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
    # Concurrency is implemented (spec/05 §1): tests dispatch as concurrent `asyncio.Task`s under
    # a shared `asyncio.Semaphore(N)`. `type=int` lets argparse reject non-numeric input on its
    # own (its own usage-error exit code, 2); "positive" is checked by hand in `main`, same
    # pattern as `_invalid_path_argument`/the `--rewrite-cache`+`--assert=plain` check just above,
    # so it can report exit code 4 with a velox-styled message instead of argparse's generic one.
    #
    # `default=None`, not `_run.DEFAULT_CONCURRENCY`: M1's `[tool.velox]` loader (spec/02 §3) can
    # also set `concurrency`, and CLI > config > built-in default (spec/02 §3) only works if
    # `main` can tell "the user typed --concurrency" apart from "argparse filled in a default" —
    # which a non-`None` default would erase. `main` folds `None` back to `DEFAULT_CONCURRENCY`
    # once it knows there's no config value either; see its concurrency-resolution comment.
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        metavar="N",
        help=f"Maximum number of tests running at once (spec/05 §1). Must be a positive "
        f"integer; 1 means exactly serial. Default: {_run.DEFAULT_CONCURRENCY}, or "
        f"[tool.velox] concurrency if set.",
    )
    # Also implemented (spec/05 §2-4): wraps each test's setup+call in `asyncio.timeout`.
    # Default is `None` (off) rather than some finite value — spec/05 §11 Q12 leaves "should the
    # default be finite" an explicit open question, and pytest itself has no default test timeout
    # either, so leaving this off keeps an existing suite's behavior unchanged until the user
    # opts in. `<= 0` and non-finite (`nan`/`inf`) values are rejected by hand in `main`, same
    # exit-4 style as `--concurrency`: `asyncio.timeout(0)`/`asyncio.timeout(-5)` neither raise nor
    # mean "no limit" — the deadline is already in the past the moment the context manager is
    # entered, so cancellation is delivered at the test's *first suspension point* and never at all
    # if it has none, making a `0`/negative "budget" mean "fail every test that happens to await
    # something" rather than "fail everything" or "no limit" — neither of which is a real, useful
    # mode, so it is rejected rather than given surprising defined behavior. `nan`/`inf` would each
    # just silently never fire. `None` already doubles as "unset" here (same trick as
    # `--concurrency` above), so `[tool.velox] timeout` slots in the same way: `main` only reaches
    # for it when the CLI flag was never given at all.
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Per-test setup+call budget, in seconds (spec/05 §2-4). A test that exceeds it is "
        "reported as TIMEOUT rather than FAILED/ERROR. Must be positive and finite. Default: no "
        "limit, or [tool.velox] timeout if set.",
    )
    # Capture is implemented (spec/09 §1): stdout/stderr are routed through a per-test Sink by
    # default, shown only for failing tests. `-s`/`--capture=no` disables that routing's
    # buffering in favor of a live pass-through, prefixed per line with the test id so concurrent
    # output stays readable (spec/09 §1) — unlike pytest's `-s`, this does *not* force serial: the
    # per-line prefix is what keeps interleaved output attributable, so `--concurrency` keeps
    # working alongside it. `--capture=no` is the long form pytest scripts already spell; `-s` is
    # the shorthand both tools share.
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
    # tmp_path/tmp_path_factory are implemented (spec/09 §5): a fresh, numbered session root by
    # default (retention: velox._capture.DEFAULT_BASETEMP_RETENTION previous roots kept), or this
    # override. Validated by hand in `main` (`_invalid_basetemp_argument`, exit code 4) before it
    # ever reaches `_capture.install`'s own `shutil.rmtree` — the one "does real work" flag whose
    # failure mode is irreversible, so the `WARNING:` below is backed by a real guard, not just a
    # help string. `_capture.install`'s `BASETEMP_MARKER_NAME` check is the second, independent
    # layer, for callers that skip `main` entirely.
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
    """Where velox looks when the user passes no paths and `[tool.velox] testpaths` isn't set.

    spec/02 §1: the default is the configured `testpaths`, else the rootdir. This is only the
    last, built-in-default tier of that chain — `main` tries `args.paths` and
    `_config.Config.testpaths` first (spec/02 §3) and falls back to this only when neither is
    set. `rootdir/tests` if it exists, `rootdir` itself otherwise; `rootdir` defaults to `cwd()`
    for callers (and the pre-`[tool.velox]` tests) that don't have one to hand. Never the
    unfiltered cwd on its own: that is what made `_discover_python_files`'s missing pruning
    reachable without the user asking for it — a bare `velox` at this repo's own root used to walk
    2345 files, 780 of them under `.venv/.../site-packages`.
    """
    # `Path()` (`.`), not `Path.cwd()`, when no `rootdir` is given: this must stay relative so
    # existing callers (this package's own tests, run with `cwd` already set to the directory
    # under test) see exactly the same `Path("tests")`/`Path()` results as before this parameter
    # existed — `Path.cwd()` would silently turn those into absolute paths instead.
    base = rootdir if rootdir is not None else Path()
    tests_dir = base / "tests"
    if tests_dir.is_dir():
        return [tests_dir]
    return [base]


def _invalid_path_argument(paths: list[str]) -> str | None:
    """The first usage error in an explicit `PATHS` list, or `None` if they all look usable.

    Two cases M0 must not swallow as "found nothing" (spec/02 §4, I8): a test id (`path.py::
    test_name`), which spec/02 §1 documents as supported invocation syntax but M0 does not parse
    yet; and a path that doesn't exist on disk at all. A path that exists but matches no test
    files is left alone — that is a legitimate, if unusual, empty selection, not a usage error.
    """
    for raw in paths:
        if "::" in raw:
            return f"test ids are not implemented yet (M0): {raw!r}"
        if not Path(raw).exists():
            return f"path does not exist: {raw!r}"
    return None


def _invalid_basetemp_argument(basetemp: Path | None) -> str | None:
    """The usage error in an explicit `--basetemp DIR`, or `None` if it looks safe enough to let
    `_capture.install` decide the rest.

    `_capture._resolve_basetemp_root` does an unguarded `shutil.rmtree` on whatever this resolves
    to, so the one thing this function exists to catch is a path shape that would make an
    ordinary typo catastrophic: an empty value (`argparse` turns `--basetemp=` into `Path("")`,
    which *is* `PosixPath(".")`), the current directory or any of its ancestors, the home
    directory, or the filesystem root — `--basetemp=`, `--basetemp=.`, `--basetemp=$PWD`,
    `--basetemp=..`, and `--basetemp=$HOME` are all this same one-keystroke mistake. Deliberately
    conservative rather than exhaustive: an existing directory that isn't obviously dangerous but
    also doesn't look like a previous velox basetemp is refused instead by
    `_capture.install`/`_resolve_basetemp_root`'s own `BASETEMP_MARKER_NAME` check, since that
    check already has to exist there anyway for direct callers of `_capture.install`/`run_suite`
    that skip `main` entirely — this function is the first, cheaper layer, not the only one.
    """
    if basetemp is None:
        return None
    resolved = basetemp.expanduser().resolve()
    if resolved == Path(resolved.anchor):
        return f"--basetemp must not be the filesystem root: {resolved}"
    try:
        home = Path.home().resolve()
    except RuntimeError:
        # No resolvable home directory (a minimal/sandboxed environment) -- nothing to compare
        # against, so this specific check is simply inapplicable rather than a reason to fail.
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


@overload
def _resolve_layered[T](cli_value: T | None, config_value: T | None, default: T) -> T: ...
@overload
def _resolve_layered[T](
    cli_value: T | None, config_value: T | None, default: None = None
) -> T | None: ...
def _resolve_layered(cli_value, config_value, default=None):
    """CLI > `[tool.velox]` > built-in default (spec/02 §3), spelled out once so every merged
    option applies the same three-tier rule instead of a hand-rolled variant per flag that could
    quietly diverge (e.g. one call site forgetting the final default fallback).

    Overloaded, not just annotated `-> T | None`, purely so a non-`None` `default` (as
    `effective_concurrency` passes) lets the type checker narrow the result to `T` instead of
    `T | None` -- `effective_timeout`, which omits `default`, correctly keeps the `T | None` it
    actually needs (spec/05 §11 Q12: no timeout is a real, meaningful value here, not an unset
    marker).
    """
    if cli_value is not None:
        return cli_value
    if config_value is not None:
        return config_value
    return default


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # `plan` returns before resolving `--rewrite-cache` in `plain` mode, so passing both is a
    # silently-ignored contradiction rather than an error. argparse's `choices=` can't express
    # "unless this other flag is set", so it's checked by hand. Exit 4: spec/02 §4, usage error.
    if args.assert_mode == "plain" and args.rewrite_cache is not None:
        print(
            "velox: --rewrite-cache has no effect with --assert=plain "
            "(plain mode never touches the rewrite cache)",
            file=sys.stderr,
        )
        return 4

    # `--basetemp` is the one "does real work" flag whose failure mode is irreversible
    # (`_capture.install` eventually `shutil.rmtree`s it) — see `_invalid_basetemp_argument`'s own
    # docstring for exactly what this rejects and why the check lives here rather than only in
    # `_capture.py` (cheaper to fail fast with a clean exit-4 message than to let collection and
    # rewrite-hook setup run first for a run that was always going to be rejected).
    basetemp_problem = _invalid_basetemp_argument(args.basetemp)
    if basetemp_problem is not None:
        print(f"velox: {basetemp_problem}", file=sys.stderr)
        return 4

    # A typo'd path and a genuinely empty suite must not look the same (I8) — without this,
    # both `velox /typo` and `velox tests/test_run.py::test_x` (the id form spec/02 §1
    # documents, unimplemented in M0) would silently walk to nothing and exit 5 "no tests
    # collected", indistinguishable from an honest empty selection. Exit 4 names the offending
    # argument instead. This is deliberately narrower than full `PATHS` validation: a directory
    # that exists but happens to contain no test files is still a legitimate (if unusual) 0-tests
    # run, not a usage error — only "doesn't exist" and "looks like an id" are rejected here.
    problem = _invalid_path_argument(args.paths)
    if problem is not None:
        print(f"velox: {problem}", file=sys.stderr)
        return 4

    # spec/02 §3: `[tool.velox]`-anchored upward search, stopping at the git root — see
    # `_config.resolve`'s own docstring for exactly where it starts and stops. A malformed
    # `pyproject.toml`/`[tool.velox]` table is always a usage error (I6: never silently fall back
    # to defaults over a config the user wrote but velox can't honor).
    try:
        config = _config.resolve([Path(p) for p in args.paths])
    except _config.ConfigError as exc:
        print(f"velox: {exc}", file=sys.stderr)
        return 4

    # CLI > [tool.velox] > built-in default (spec/02 §3), in that order, via one shared helper —
    # `args.concurrency`/`args.timeout` are `None` exactly when the flag wasn't given (see
    # `build_parser`'s comments on both), so this is the same three-tier resolution for both
    # rather than two hand-spelled variants that could quietly drift apart (e.g. one gaining the
    # built-in-default fallback the other forgets).
    effective_concurrency = _resolve_layered(
        args.concurrency, config.concurrency, _run.DEFAULT_CONCURRENCY
    )
    effective_timeout = _resolve_layered(args.timeout, config.timeout)

    # Same two checks the raw `args.concurrency`/`args.timeout` used to get, just moved to run
    # against the merged value — a bad number is exactly as much a usage error coming from
    # `[tool.velox]` as from the command line, and `run_suite`'s own `ValueError` (defense in
    # depth for direct callers) is too late here to produce a clean exit code either way. Named by
    # its actual source (the CLI flag, or the config file that set it) rather than always saying
    # `--concurrency`/`--timeout` — a bad `[tool.velox] concurrency` shouldn't point the user at a
    # flag they never touched.
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

    # spec/02 §1: `PATHS` > configured `testpaths` > the rootdir. `config.testpaths` entries are
    # written relative to wherever `[tool.velox]` was declared, so they're resolved against
    # `config.rootdir` here, not `cwd()` — the same reason `_default_test_roots` below takes
    # `config.rootdir` rather than defaulting to `cwd()` itself.
    # `is not None`, not truthiness: `testpaths = []` is a real, if unusual, thing to write and
    # means "nothing" -- collapsing it into "unset" would silently run the built-in default
    # instead of the empty selection the user's config actually asked for.
    if args.paths:
        roots = [Path(p) for p in args.paths]
    elif config.testpaths is not None:
        roots = [config.rootdir / p for p in config.testpaths]
        # Mirrors `_invalid_path_argument`'s I8 reasoning for CLI paths: a typo'd `testpaths`
        # entry must not silently look like an honest empty selection either. Unlike CLI paths,
        # nothing upstream of this point has ever checked `config.testpaths` against the
        # filesystem, so it's done here, right before these roots are actually used.
        for root, raw in zip(roots, config.testpaths, strict=True):
            if not root.exists():
                print(
                    f"velox: {config.source}: testpaths entry does not exist: {raw!r}",
                    file=sys.stderr,
                )
                return 4
    else:
        roots = _default_test_roots(config.rootdir)

    # Resolved and probed up front so the cold-start guarantee (spec/07 §5) is visible before
    # a run commits to it — a benchmark that silently fell back to `plain` is a corrupted
    # benchmark. `plan` warns on stderr; the header line goes to stdout with the report.
    setup = _rewrite.plan(roots, mode=args.assert_mode, cache_dir=args.rewrite_cache)
    print(setup.header_line())
    # Same transparency `plan`'s own header line gives the assertion-rewrite decision — a run
    # silently picking up config the user forgot was there (or forgot to write) is exactly the
    # kind of surprise I6 exists to name instead of hide.
    print(f"config: {config.source}" if config.source is not None else "config: none")

    rootdir = config.rootdir

    # `env_backup`/the matching restore loop in this `try`'s `finally` (not `monkeypatch` — this
    # is production code, not a test) are what make this safe to call repeatedly in-process (this
    # package's own test suite does exactly that): every key `env` touches is restored to its
    # pre-call value (or removed, if it didn't exist before) on the way out, so one `main()`
    # call's config can never leak into the next one's environment, or into an embedding process
    # that called `main()` for a side effect other than exiting. Unconditional overwrite of each
    # named key: there is no CLI flag for an individual `env` entry to take precedence over, so
    # `[tool.velox] env` is the only source and always wins for the keys it names.
    #
    # Deliberately the *first* thing inside this `try`, ahead of `_rewrite.install` and
    # everything else it guards: spec/02 §5 only requires `env` to land before the first test
    # module import, but putting the mutation itself inside the same `try`/`finally` that
    # restores it (rather than just before it, as an earlier version of this code did) means the
    # restore now fires even if `_rewrite.install` itself raises, not only for exceptions raised
    # after it succeeds.
    env_backup = {key: os.environ.get(key) for key in config.env}
    os.environ.update(config.env)

    # Set here, not just inside the `try` below: if `_rewrite.installed_hook()` itself somehow
    # raised before reassigning this, the `finally`'s `if not hook_already_installed:` would
    # otherwise hit an unbound name instead of the original exception. `False` is also the safer
    # fallback value for that case -- it makes `finally` attempt an `uninstall()`, not skip one.
    hook_already_installed = False
    try:
        # Must be installed before any test module is imported below — a module already sitting
        # in `sys.modules` can't retroactively be rewritten. `warn` already happened inside `plan`
        # above, so this call is handed the decision it made rather than re-probing the cache.
        #
        # Not fixed here: `install` walks every `.py` under `roots` for its own `_initialpaths`
        # (`_discover_python_files`) and `discover_files` below walks the same roots again for
        # test files specifically — two full traversals per run, against I7's 50ms startup
        # budget. They are not the same walk (one wants every `.py`, the other only
        # `test_*.py`/`*_test.py`), so unifying them means changing `_rewrite.install`'s
        # signature to accept a pre-discovered file list rather than discovering its own — real
        # surgery in a module this pass wasn't scoped to restructure, and secondary to
        # `_import_module` actually consulting the hook at all (the correctness bug, now fixed).
        # Left as a known, named cost, worth revisiting once a shared "test tree walker" exists
        # for `[tool.velox]` config to hang off of too.
        hook_already_installed = _rewrite.installed_hook() is not None
        _rewrite.install(roots, setup=setup, warn=False)
        # `config.test_file_patterns`/`config.ignore` replace `discover_files`'s own defaults
        # outright when set, matching spec/02 §3's example table (`ignore = [...]` there spells
        # out the exact built-in default set, not an addition to it) — a user who wants "the
        # defaults plus one more" repeats the defaults themselves, the same convention this
        # module's own `--capture`/`--assert` choices don't need but a list-valued config key
        # does. `is not None`, not truthiness, for both: `test_file_patterns = []`/`ignore = []`
        # are real, if unusual, things to write and mean "none" -- the same reasoning
        # `config.testpaths`'s own resolution above already spells out, and the same mistake
        # (`x or DEFAULT`, which silently swaps back to the default for an explicit `[]`) it warns
        # against.
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
        collected = _collect.collect(files, rootdir=rootdir)
        capture_passthrough = args.capture == "no" or args.capture_s
        # Populated by `run_suite` iff non-`None` (spec/09 §9 "MVP" mentions this section
        # explicitly) — see `_capture.py`'s module docstring for exactly what can land here under
        # this runtime (a genuinely detached background thread; end-of-run session-scope
        # teardown output). Empty in the overwhelming common case. Rendered by `reporter.finish`
        # below, not printed here directly — see `_report.py`'s module docstring for why that
        # move keeps every "what got printed and in what order" decision in one place.
        unattributed: list[str] = []

        # `Reporter` groups `TestResult`s (which carry no path of their own, spec/10's
        # `_report.py` docstring) back into per-file blocks by walking `collected.records` itself
        # — handed to it directly, not reduced to an `{id: path}` dict first: a dict comprehension
        # keyed by `record.id` would silently collapse two records sharing an id (an ordinary,
        # reachable case — a factory-generated test repeats its `func.__qualname__`, hence its id,
        # for every instance it produces), undercounting that file's expected test total. See
        # `Reporter`'s own docstring for the full reasoning; this is the caller half of it — the
        # same `collected.records` list `run_suite` below is about to dispatch, so every id
        # `Reporter.on_result` is ever handed is guaranteed to be one of `records`'s.
        reporter = _report.Reporter(
            records=collected.records,
            capture_passthrough=capture_passthrough,
            stream=sys.stdout,
        )

        # Wall clock around the whole `run_suite` call, not derived from summing per-test
        # durations afterwards — `reporter.finish`'s wall-vs-Σ line (spec/10 §2) is exactly the
        # comparison between this real elapsed time and that sum, so the two must be measured
        # independently for the ratio to mean anything.
        #
        # Known, deliberate gap, in the same "name it rather than let it be reported as a velox
        # bug" spirit spec/10 §4 already asks of JUnit's `<testsuite time>`: this window is wider
        # than "pure execution". It brackets the whole `run_suite` call, which does real
        # non-execution work at both ends — `_capture.install()`'s basetemp `mkdir`/marker
        # write/retention sweep going in, `store.aclose()`/`executor.shutdown()`/`runner.close()`/
        # `_capture.uninstall()` coming out — so the concurrency ratio on `finish`'s final line is
        # very slightly deflated by that overhead (measured on an 8-test suite each awaiting 10ms:
        # 6.94x reported against 7.27x for execution alone), and, for a suite whose basetemp
        # retention sweep has real garbage to clear, potentially by much more than "slightly". It
        # also *excludes* discovery, import and collection entirely, which are typically the
        # largest single chunk of a small run — the right choice for a Σ/wall ratio specifically,
        # but it means this `Ns wall` is not the number `time velox` would report. A precise fix
        # needs `run_suite` to report its own inner span (taken immediately around
        # `runner.run(run_all())`) separately from this outer one, which touches `_run.py`'s
        # already-dense interrupt/teardown-ordering control flow — left as a named, measured cost
        # rather than a change bundled into an unrelated pass.
        wall_start = time.monotonic()
        results = _run.run_suite(
            collected.records,
            concurrency=effective_concurrency,
            timeout=effective_timeout,
            capture_passthrough=capture_passthrough,
            basetemp=args.basetemp,
            unattributed_output=unattributed,
            on_result=reporter.on_result,
        )
        wall_clock = time.monotonic() - wall_start

        # `reporter.finish(...)` is called *last*, after every other end-of-run section below —
        # deliberately reordered (this slice) so spec/10 §2's "final line" claim for the wall-vs-Σ
        # line is actually true of `cli.main`'s output, not just of `Reporter.finish`'s own return.
        # Before this reorder, the skip list, a full traceback per collection error, and the
        # `N tests: ...` summary line below all printed *after* `finish`, burying the headline
        # metric spec/10 §2 calls the proof-of-value number above several screens of detail.
        for skipped in collected.skipped:
            print(f"{skipped.id} SKIPPED ({skipped.reason})")

        for error in collected.errors:
            print(f"{error.path} COLLECTION ERROR")
            print(error.message)

        passed = sum(1 for result in results if result.outcome is _run.Outcome.PASSED)
        failed = sum(1 for result in results if result.outcome is _run.Outcome.FAILED)
        errored = sum(1 for result in results if result.outcome is _run.Outcome.ERROR)
        # Named explicitly, same reasoning as `passed`/`failed`/`errored`: `Outcome.TIMEOUT` is
        # its own outcome (see `_run.Outcome.TIMEOUT`'s docstring for why it isn't folded into
        # `FAILED`/`ERROR`, and for how `_run._run_one` now makes sure a genuinely-exceeded budget
        # is reliably classified `TIMEOUT` rather than `FAILED`/`ERROR` even when the test's own
        # code intercepts the cancellation), so the summary line should say so too rather than let
        # it fall into the `other` catch-all below.
        timed_out = sum(1 for result in results if result.outcome is _run.Outcome.TIMEOUT)
        # `other` exists so this line can't silently stop adding up to `len(results)`: the four
        # named `sum()`s above are each independent counts, not `len(results) - the rest` the way
        # a two-outcome world could get away with, so a future `Outcome` member (`skipped`/
        # `xfailed`/..., spec/05 §4) that starts reaching `run_suite`'s results before this line
        # is updated for it shows up here as a nonzero "other" bucket instead of vanishing from
        # the total with nothing to say the count is now wrong.
        other = len(results) - passed - failed - errored - timed_out
        summary = f"{len(results)} tests: {passed} passed, {failed} failed, {errored} errored"
        if timed_out:
            summary += f", {timed_out} timed out"
        if other:
            summary += f", {other} other"
        summary += (
            f", {len(collected.skipped)} skipped, {len(collected.errors)} collection error(s)"
        )
        print(summary)

        # This line and `reporter.finish`'s wall-vs-Σ line are two summaries of the same run that
        # use "failed" for two different sets on purpose, not by drift: this one is the pre-existing
        # per-`Outcome` breakdown (`Outcome.FAILED` specifically, distinct from `errored`/
        # `timed_out` — a distinction real for debugging, per `_run.Outcome`'s own docstrings), and
        # `finish`'s is spec/10 §2's coarser proof-of-value count (every non-`PASSED` outcome, to
        # match its failure-details/short-summary sections above it). Both are correct for what
        # each is for; naming the potential-for-confusion here is cheaper and more honest than
        # collapsing the finer breakdown into the reporter's line, or dropping this one, in a pass
        # that was not scoped to redesign the summary format.
        reporter.finish(results, wall_clock=wall_clock, unattributed_output=unattributed)

        return _run.exit_code_for(results, collected.errors, skipped=len(collected.skipped))
    finally:
        # `main` is called repeatedly in-process (this package's own test suite does exactly
        # that), and an embedding caller may too (I1) — leaving the hook on `sys.meta_path`
        # after this call returns would leak global state into whatever runs next. This must
        # fire on every exit path from here, including an exception bubbling out of collection
        # or execution; nothing above is caught, so a real velox bug still surfaces as one.
        #
        # Only torn down if this call is the one that put it there: `_rewrite.install` is a
        # documented no-op when a hook is already on `sys.meta_path`, so an embedder (or a
        # nested `main()`) that installed its own hook first must keep it — unconditionally
        # calling `uninstall()` would remove someone else's hook and `set_cache_root(None)`
        # global state that isn't this call's to clear. Two `main()` calls racing on different
        # threads over the same module-global hook is a real gap this doesn't close either, but
        # it is the same gap `_rewrite.py`'s own module-global `_installed_setup` already has —
        # not something introduced here, and not fixed here.
        if not hook_already_installed:
            _rewrite.uninstall()
        # Symmetric with `env_backup`'s own comment above: restores exactly the keys this call
        # touched, to exactly what they were before it touched them (or removes them, if they
        # didn't exist), regardless of how this `try` exits.
        for key, prev_value in env_backup.items():
            if prev_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev_value


if __name__ == "__main__":
    sys.exit(main())
