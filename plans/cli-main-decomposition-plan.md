# `cli.py`: decompose `main`'s execute phase

Internal working document. Follow-on to the `refactor-cli` branch (merged 2026-09-07), which pulled
`main`'s usage-error/config/option-resolution preamble out into `_prepare_run` (see
[cli.py:665-836](../velox/cli.py#L665-L836)). That left `main` itself
([cli.py:819](../velox/cli.py#L819)) at ~530 lines covering the run: process-global setup, hook
install, discovery, collection, `run_suite`, reporting, and the cache/index writes at the end.
Reviewed with `/simplify` at the time; this plan is the two follow-ups that review's altitude
finding raised but the merged PR intentionally left out as bigger than that pass's scope.

No file split proposed — `velox/CLAUDE.md` pins `cli.py` flat at the package root. Both PRs below
extract functions within the file, same as `_prepare_run` did.

---

## PR 1 — pull the end-of-run cache/index settlement into a pure function

[cli.py:1283-1334](../velox/cli.py#L1283-L1334): the block from `resolved_rootdir = rootdir.resolve()`
through the `_index.save` call. Six chained set/dict computations (`attempted`, `answered`,
`errored`, `gone`, `produced`, `settled_ids`) decide what `_cache.merge` and `_index.refresh` get
handed, each with a paragraph of reasoning about what a partial or interrupted run does or doesn't
settle. It's also the densest, most correctness-sensitive logic in the file, and it needs nothing
but values `main` already has by that point — `results`, `collected`, `found`, `files`,
`discovered`, `last_run`, `roots`, `rootdir` — no import hook, no `sys.path`, no live test run. It
is exactly the kind of logic a unit test should pin down directly, and currently the only way to
exercise it is a full `main()` invocation.

Work: extract a function (name and exact signature TBD when written — something like
`_settle(results, collected, found, *, files, discovered, last_run, roots, rootdir)` returning
whatever `_cache.merge`/`_index.refresh`/`_index.save` need) and add direct unit tests against it —
a cancelled test, a vanished file, a `--lf`-narrowed run, a file that half-imported — using
hand-built `TestResult`/`CollectionResult`/cache fixtures instead of a real run.

Verify: `just check`.

## PR 2 — wrap the install/uninstall triad in a context manager

[cli.py:934-1013](../velox/cli.py#L934-L1013) sets up (env vars, `sys.path`, the assertion-rewrite
import hook, the warnings shim) and [cli.py:1344](../velox/cli.py#L1344)'s `finally` tears the same
four things back down, tracked by three hand-set booleans (`hook_already_installed`,
`warnings_installed`, `sys_path_inserted`) so a nested `main()` call or a raise mid-setup doesn't
remove something it didn't install. That's a context manager's job. Wrapping it as one (e.g.
`with _installed_session(config, roots, setup, args.filterwarnings) as session:`) shrinks `main`
and turns the three-flags-plus-`try`/`finally` pattern into one reusable, testable primitive —
tested for install/uninstall symmetry (including on an exception) by mocking `_rewrite`/`_warnings`,
rather than by reasoning through `main` end to end.

This one doesn't make anything newly *unit-testable* the way PR 1 does — the underlying operations
(env mutation, import hooks) are inherently process-global and stay integration-tested — it's a
readability/reliability win on the invariant itself.

Verify: `just check`; this touches `main`'s own regression coverage (nested `main()` calls,
`--co-json`'s stderr-only warnings path) most directly, so re-run those cases by hand too.

---

## Out of scope

- Splitting `cli.py` into a package — `velox/CLAUDE.md` rules this out.
- The collect-only reporting duplication (fast index-answer path vs. real-collection path, both
  building the same JSON/plain report + exit status shape) — noted during the `/simplify` review as
  a smaller, opportunistic cleanup, not folded in here since neither PR above touches that code.
