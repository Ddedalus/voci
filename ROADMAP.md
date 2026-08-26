# Roadmap

velox is pre-v0.1. This page is the single honest list of what isn't finished — if a feature isn't
mentioned in [README.md](README.md) or [docs/](docs/), assume it's here.

## Working today

Discovery and collection, including `class Test*` grouping and a diagnostic for test shapes that
would otherwise collect as nothing · explicit dependency injection with
`call`/`function`/`module`/`session`
scopes, single-flight construction and inverted teardown · concurrent execution under a semaphore
with per-test timeouts, overridable with `@velox.timeout(...)` · `exclusive=` on a fixture and
`@velox.solo`, admission-controlled against everything else running · `@velox.isolated`'s
per-test subprocess tier · `skip`/`skipif`/`xfail`, conditional or not ·
`@velox.parametrize`, including stacked decorators and `velox.case(...)` marks on one case ·
parametrized fixtures (`params=` on `@velox.fixture`) · `velox.use(...)` fixture declarations
on a test module or a package `__init__.py` ·
`unittest.mock` patch detection, solo scheduling and its reported cost ·
selection by `path.py::test_name` id, `-k` and `@velox.tag` with `-m` · `-x`/`--maxfail`
and Ctrl-C, both cancelling what is in flight · `--serial`, `--collect-only`, `-v`/`-q` and
`--durations` · assertion introspection with comparison
diffs · stdout/stderr/logging capture and `tmp_path` · the event-loop watchdog and the failure a
test earns for returning a value or dropping a coroutine un-awaited · the reporter · `--report-json`
· `[tool.velox]` config · `velox.fastapi` per-test dependency overrides.

## Next

**Codegen**: velox-migrate, plans:
High-level in `plans/migration-tool-plan.md`

Next: convert and verify httpx2 (successor to httpx, active development), audited in
`plans/httpx2-audit.md` and green under pytest at 1973 tests.

## docs
High-level plan in `plans/docsite-plan.md`

**Performance.** A persistent collection cache, `--lf`/`--ff`,

**Coverage.** Ensuring we play nicely with coverage and can produce suitable reports.

## Code quality consolidation

Code review of velox so far, coverage gaps, clean CI, drop fat, identify duplication etc.

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

**Reporting.** JUnit XML and GitHub annotations.


### Needs human review

**Testmon functionality** Allow for coverage-driven replay of only affected tests in a suite after code modification.
 
**Benchmark** A published, reproducible
benchmark against pytest and `pytest-xdist` on a real suite.

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

## Not planned

A plugin and hook system — dependency injection is the extension point, and zero hook dispatch on
the hot path is a design invariant. pytest syntax compatibility. `unittest`/`doctest`/`nose`
collection. Multi-process execution; `@velox.isolated` is the only subprocess path, though results
are serializable so the door stays open. First-class Windows support.
