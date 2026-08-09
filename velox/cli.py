"""Command-line entrypoint for velox.

M0 (spec/00 §8): discover, import, run, print pass/fail, correct exit code. No `-k`/`-m`/
`--collect-only` selection beyond what's already here — those are later milestones; see
`_discovery`, `_collect`, and `_run` for the pieces this wires together.

M1 concurrency slice (spec/05 §1-4): `--concurrency` and `--timeout` are now real, wired straight
through to `_run.run_suite`, alongside `--assert`/`--rewrite-cache` in the "does real work"
category this docstring already calls out.

M1 capture slice (spec/09): `-s`/`--capture=no` and `--basetemp` join that same category. Printing
captured output is deliberately minimal here — a failing test's stdout/stderr/log records below
its traceback, and an "unattributed output" section if the session sink caught anything — the full
jest-style reporter (per-file blocks, live footer, `--durations`, JUnit) is spec/10, not this
milestone.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from velox import __version__, _capture, _collect, _discovery, _rewrite, _run


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
    parser.add_argument(
        "--concurrency",
        type=int,
        default=_run.DEFAULT_CONCURRENCY,
        metavar="N",
        help=f"Maximum number of tests running at once (spec/05 §1). Must be a positive "
        f"integer; 1 means exactly serial. Default: {_run.DEFAULT_CONCURRENCY}.",
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
    # just silently never fire.
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Per-test setup+call budget, in seconds (spec/05 §2-4). A test that exceeds it is "
        "reported as TIMEOUT rather than FAILED/ERROR. Must be positive and finite. Default: no "
        "limit.",
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
    # override.
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


def _default_test_roots() -> list[Path]:
    """Where velox looks when the user passes no paths.

    spec/02 §1: the default is the configured `testpaths`, else the rootdir. velox has no
    `[tool.velox]` loader yet — nothing in this package reads `pyproject.toml` — so this is only
    the built-in-default tier of that chain: `./tests` if it exists, the current directory
    otherwise. A `[tool.velox]` loader, once it exists, slots in ahead of this rather than
    replacing it. Never the unfiltered cwd on its own: that is what made
    `_discover_python_files`'s missing pruning reachable without the user asking for it — a bare
    `velox` at this repo's own root used to walk 2345 files, 780 of them under
    `.venv/.../site-packages`.
    """
    tests_dir = Path("tests")
    if tests_dir.is_dir():
        return [tests_dir]
    return [Path()]


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

    # `type=int` above already rejects non-numeric input (argparse's own exit code 2); this is
    # the "positive" half, checked by hand so a bad value gets the same velox-styled exit-4 usage
    # error as every other check in this function rather than argparse's differently-shaped one.
    # `run_suite` itself also raises `ValueError` for `concurrency < 1` (defense in depth for
    # callers that skip `main`, e.g. tests calling it directly), but by the time that would fire
    # here it's too late to produce a clean exit code — this check is what actually stops a bad
    # `--concurrency` from ever reaching it.
    if args.concurrency < 1:
        print(
            f"velox: --concurrency must be a positive integer, got {args.concurrency}",
            file=sys.stderr,
        )
        return 4

    # Same shape as the `--concurrency` check above, for the same reason: reject rather than give
    # `0`/negative/non-finite a surprising defined meaning (see `build_parser`'s comment on this
    # flag). `run_suite` itself also raises `ValueError` for the same condition (defense in depth
    # for direct callers), but again, too late here to produce a clean exit code on its own.
    if args.timeout is not None and not (math.isfinite(args.timeout) and args.timeout > 0):
        print(
            f"velox: --timeout must be a positive, finite number of seconds, got {args.timeout}",
            file=sys.stderr,
        )
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

    roots = [Path(p) for p in args.paths] if args.paths else _default_test_roots()

    # Resolved and probed up front so the cold-start guarantee (spec/07 §5) is visible before
    # a run commits to it — a benchmark that silently fell back to `plain` is a corrupted
    # benchmark. `plan` warns on stderr; the header line goes to stdout with the report.
    setup = _rewrite.plan(roots, mode=args.assert_mode, cache_dir=args.rewrite_cache)
    print(setup.header_line())

    # rootdir: spec/02 §3's `[tool.velox]`-anchored upward search (stopping at the git root)
    # doesn't exist yet — there is no config loader in this package at all. `cwd` is the honest
    # placeholder until that lands, matching the same not-yet-built admission
    # `_default_test_roots` already makes about `testpaths`.
    rootdir = Path.cwd()

    # Must be installed before any test module is imported below — a module already sitting in
    # `sys.modules` can't retroactively be rewritten. `warn` already happened inside `plan`
    # above, so this call is handed the decision it made rather than re-probing the cache.
    #
    # Not fixed here: `install` walks every `.py` under `roots` for its own `_initialpaths`
    # (`_discover_python_files`) and `discover_files` below walks the same roots again for
    # test files specifically — two full traversals per run, against I7's 50ms startup budget.
    # They are not the same walk (one wants every `.py`, the other only `test_*.py`/`*_test.py`),
    # so unifying them means changing `_rewrite.install`'s signature to accept a pre-discovered
    # file list rather than discovering its own — real surgery in a module this pass wasn't
    # scoped to restructure, and secondary to `_import_module` actually consulting the hook at
    # all (the correctness bug, now fixed). Left as a known, named cost, worth revisiting once
    # a shared "test tree walker" exists for `[tool.velox]` config to hang off of too.
    hook_already_installed = _rewrite.installed_hook() is not None
    _rewrite.install(roots, setup=setup, warn=False)
    try:
        files = _discovery.discover_files(roots)
        collected = _collect.collect(files, rootdir=rootdir)
        capture_passthrough = args.capture == "no" or args.capture_s
        # Populated by `run_suite` iff non-`None` (spec/09 §9 "MVP" mentions this section
        # explicitly) — see `_capture.py`'s module docstring for exactly what can land here under
        # this runtime (a genuinely detached background thread; end-of-run session-scope
        # teardown output). Empty in the overwhelming common case.
        unattributed: list[str] = []
        results = _run.run_suite(
            collected.records,
            concurrency=args.concurrency,
            timeout=args.timeout,
            capture_passthrough=capture_passthrough,
            basetemp=args.basetemp,
            unattributed_output=unattributed,
        )

        # `Outcome.value` ("passed"/"failed"/"error") upper-cased rather than a three-way
        # if/elif: adding a further outcome (skipped/xfailed/... , spec/05 §4) later must not
        # require touching this line again to keep printing it correctly.
        for result in results:
            print(f"{result.id} {result.outcome.value.upper()} ({result.duration:.3f}s)")
            if result.failure is not None:
                print(result.failure)
            # spec/09 §6: captured output is only ever non-empty on `TestResult` for a failing
            # outcome (`run_suite`'s own `dispatch_one` drops it for `PASSED` before this point),
            # so no `result.outcome is not PASSED` guard is needed here beyond "is there anything
            # to print" — this is deliberately minimal (three flat sections, no truncation-aware
            # layout, no per-file grouping) since the real reporter is spec/10, out of scope here.
            if result.captured_stdout:
                print("--- captured stdout ---")
                print(result.captured_stdout)
            if result.captured_stderr:
                print("--- captured stderr ---")
                print(result.captured_stderr)
            if result.log_records:
                print("--- captured log records ---")
                for record in result.log_records:
                    print(f"{record.levelname} {record.name}: {record.getMessage()}")

        if unattributed:
            print(
                "--- unattributed output (produced outside any test's context; spec/09 §9 Q4) ---"
            )
            for section in unattributed:
                print(section)

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


if __name__ == "__main__":
    sys.exit(main())
