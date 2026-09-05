# Roadmap

This page is the work ledger. Stuff that already exists is: a) in `README.md`, b) in `docs/`, c) in the codebase.

# Next items

## Bugs and workspace issues - top priority

None known.

## Features

### Collection index
An index of `relpath → {mtime_ns, size, ids, lines}` written alongside the run cache and
invalidated per file by `(mtime_ns, size)`, so `--collect-only` and the test count answer without
importing anything. It is also what `--co-json` (machine-readable collection, for editor
integrations) and a watch mode (`--watch`: the index, a file watcher, and `--lf` ordering) would
be built on.

### velox-migrate
`plans/migration-tool-plan.md`

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

## trio support
A `@velox.trio` mark running a trio test on its own `trio.run`, inside the existing worker-thread
pool sync tests already use — not a backend-agnostic runner. Costed and decided in
`plans/trio-support-plan.md`, 3–5 days; fixture injection and external cancellation (`--maxfail`,
Ctrl-C) stay best-effort, same as a sync test today.


## `just build` fails whenever `.venv-3.13` exists
`uv build`'s sdist walks `.venv-3.13` and chokes on its absolute `bin/python` symlink; the venv is
invisible to git only through the `*` in its own `.gitignore`, which hatchling's sdist inclusion
does not read. `.venv` escapes by name, `.venv-3.13` does not. Any checkout that has run
`just py sync 3.13` — the default state after following CLAUDE.md — cannot build.

## Not planned

A plugin and hook system — dependency injection is the extension point, and zero hook dispatch on
the hot path is a design invariant. pytest syntax compatibility. `unittest`/`doctest`/`nose`
collection. Multi-process execution; `@velox.isolated` is the only subprocess path, though results
are serializable so the door stays open. First-class Windows support.
