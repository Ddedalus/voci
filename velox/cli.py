"""Command-line entrypoint for velox.

M0 (spec/00 §8): discover, import, run, print pass/fail, correct exit code. No fixtures, no
concurrency, no `-k`/`-m`/`--collect-only` selection beyond what's already here — those are
later milestones; see `_discovery`, `_collect`, and `_run` for the pieces this wires together.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from velox import __version__, _collect, _discovery, _rewrite, _run


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

    # Review: `PATHS` are accepted without validation, and spec/02 §1's id form is silently
    # mis-handled. `velox /typo` and `velox tests/test_run.py::test_x` both walk to nothing and
    # exit 5 "no tests collected" (verified) — a typo and an empty suite are indistinguishable,
    # and the id form (documented as supported invocation syntax) fails as a missing path
    # rather than as "ids aren't in M0 yet". Exit 4 with the offending argument named is the
    # spec/02 §4 answer; either way I8 says this must not resolve to a quiet count of zero.
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
    # Review: `install` walks the roots once (`_discover_python_files`, a full `os.walk` for
    # every `.py`) and `discover_files` below walks them again — two complete traversals of
    # the test tree per run, before a single test is imported. I7 budgets 50 ms
    # from process start to first dispatch; on a large monorepo root this is the first thing
    # that will blow it. The two walks want to be one, with `_initialpaths` fed from it.
    _rewrite.install(roots, setup=setup, warn=False)
    try:
        files = _discovery.discover_files(roots)
        collected = _collect.collect(files, rootdir=rootdir)
        results = _run.run_suite(collected.records)

        for result in results:
            status = "PASSED" if result.outcome is _run.Outcome.PASSED else "FAILED"
            print(f"{result.id} {status} ({result.duration:.3f}s)")
            if result.failure is not None:
                print(result.failure)

        for error in collected.errors:
            print(f"{error.path} COLLECTION ERROR")
            print(error.message)

        passed = sum(1 for result in results if result.outcome is _run.Outcome.PASSED)
        failed = len(results) - passed
        print(
            f"{len(results)} tests: {passed} passed, {failed} failed, "
            f"{len(collected.errors)} collection error(s)"
        )

        return _run.exit_code_for(results, collected.errors)
    finally:
        # `main` is called repeatedly in-process (this package's own test suite does exactly
        # that), and an embedding caller may too (I1) — leaving the hook on `sys.meta_path`
        # after this call returns would leak global state into whatever runs next. This must
        # fire on every exit path from here, including an exception bubbling out of collection
        # or execution; nothing above is caught, so a real velox bug still surfaces as one.
        #
        # Review: unconditional `uninstall()` tears down a hook `main` may not have installed.
        # `_rewrite.install` is a documented no-op when a hook is already on `sys.meta_path`,
        # so an embedder (or a nested/concurrent `main()`) that installed its own hook first
        # has it removed here, along with `set_cache_root(None)` — the "don't leak global
        # state" fix reaches into state that isn't ours. Uninstall only what this call
        # installed (`install` returns the setup; `installed_hook()` before/after tells you
        # whether it was yours) — and note that two `main()` calls on different threads race
        # over the same module-global hook regardless.
        _rewrite.uninstall()


if __name__ == "__main__":
    sys.exit(main())
