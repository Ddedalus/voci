# Roadmap

This page is the work ledger. Stuff that already exists is: a) in `README.md`, b) in `docs/`, c) in the codebase.

# Next items

## Bugs and workspace issues - top priority

None known.

## Features

### `--co-json` and watch mode
The collection index (`velox._collection.index`, invalidated per file by `(mtime_ns, size)`) now
backs `--collect-only`'s fast path. Still to build on it: `--co-json` (machine-readable collection,
for editor integrations) and a watch mode (`--watch`: the index, a file watcher, and `--lf`
ordering).

### velox-migrate
`plans/migration-tool-plan.md`

Correctness gaps, duplication, and hardcoded plugin-specific mechanisms found by review, in
`plans/migrate-review-followups.md`.

Two migration skills (`unwind-override`, `concurrency-triage`) are written but unmerged, on branch
`vx-migrate-skills` (worktree `velox-wt-migrate-skills`) — see task 14 in
`plans/migration-tool-plan.md`.

### docs
`plans/docsite-plan.md`


### Code quality consolidation

Code review of velox so far, coverage gaps, clean CI, drop fat, identify duplication etc.

### Reporting
JUnit XML and GitHub annotations

### Needs human review - do not start

**Testmon functionality** Allow for coverage-driven replay of only affected tests in a suite after code modification.
 
**Benchmark** A published, reproducible benchmark against pytest and `pytest-xdist` on a real suite.

**Injection ergonomics.** Lazy or optional dependencies, and overriding one fixture for a subtree
of tests without hand-duplicating everything downstream of it.

Held for a design decision rather than for effort. Overriding rewires a graph on behalf of code
that cannot see the change: a `scope="session"` fixture two hops downstream of a substitution
constructs once per override set, with nothing local telling its author so. `params=` on a fixture
multiplies its dependents the same way, so the question to settle first is how much implicit
specialization velox wants in total — and whether the answer here is a named specialization object
that keeps the wiring visible at the call site, or nothing at all.

Migration codegen depends on the outcome: with no override mechanism, a translated conftest
override needs a full specialized fixture chain per override scope, which
[plans/migration-problem-statement.md](plans/migration-problem-statement.md) §4.2 measures as the
largest single source of hand edits in a migrated suite.

## Later

## Starvation-aware scheduling
`exclusive=`/`@velox.solo` admission has no fairness guarantee: a
steady stream of ordinary tests can keep a waiting solo or exclusive-resource test from ever
seeing an opening.

## An `@velox.isolated` subprocess has no deadline of its own
`--timeout` is handed to the subprocess's own `run_suite` call, which arms it around the test —
so nothing bounds the time *before* that: interpreter startup, importing the test module, and
re-collecting it. A module that hangs at import leaves the parent waiting on `communicate()`
forever, holding that test's admission slot, with no way out but Ctrl-C. Bounding it from the
parent needs a decision on what the budget covers, since a legitimately slow import (a module
pulling in a large dependency) must not read as a timed-out test.

* Multi-process execution; `@velox.isolated` is the only subprocess path, though results are erializable so the door stays open.
* First-class Windows support.

## trio support
`plans/trio-support-plan.md`, 3–5 days;

## Not planned

 * A plugin and hook system — dependency injection is the extension point
 * pytest syntax compatibility.
 * `unittest`/`doctest`/`nose`
collection.
