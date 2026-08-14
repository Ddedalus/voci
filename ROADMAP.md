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
per-test subprocess tier · `skip`/`skipif`/`xfail` ·
`@velox.parametrize`, including stacked decorators · parametrized fixtures (`params=` on
`@velox.fixture`) · `velox.use(...)` fixture declarations on a test module or a package
`__init__.py` ·
`unittest.mock` patch detection, solo scheduling and its reported cost ·
selection by `path.py::test_name` id, `-k` and `@velox.tag` with `-m` · `-x`/`--maxfail`,
`--serial`, `--collect-only`, `-v`/`-q` and `--durations` · assertion introspection with comparison
diffs · stdout/stderr/logging capture and `tmp_path` · the reporter · `[tool.velox]` config ·
`velox.fastapi` per-test dependency overrides.

## Next

**DX Improvements** This is how the output currently looks like:

```
config: pyproject.toml
PASS  tests/test_orders.py               13 tests   Σ 4.91s
PASS  tests/test_users.py                10 tests   Σ 2.42s
tests/test_users.py::test_list_users_is_paginated SKIPPED (pagination is not implemented yet (GET /users has no route -- 405, not 200))
tests/test_users.py::test_response_carries_request_id SKIPPED (middleware is behind a feature flag)
25 tests: 23 passed, 0 failed, 0 errored, 2 skipped, 0 collection error(s)

25 tests · 0 failed · 1.77s wall (4.1x concurrency)
```

Feedback:
1. Skips take too much place, they should just be counted in the per-file line entry
2. The space left for paths is too small for real life - increase unless it's dynamically calculated.
3. There are two summary lines which duplicate info - this is nonsense. For success, compress to one line, do not show the zero counts, so something like:
```
25 tests · 23 passed · 2 skipped · 1.77s wall (4.1x concurrency)
```
For failure, you can have two lines: one for the failures and one for the summary. I'd also accept other sensible layouts as you dig through the UX.

**Starvation-aware scheduling.** `exclusive=`/`@velox.solo` admission has no fairness guarantee: a
steady stream of ordinary tests can keep a waiting solo or exclusive-resource test from ever
seeing an opening.

**Runtime safety.** A loop-starvation watchdog that names the blocking call instead of letting the
suite mysteriously stall; failing a test that returns a value or leaves a coroutine un-awaited;
cancelling in-flight tests, with time-boxed teardown, on Ctrl-C and when `--maxfail` is reached
(which today stops new tests from starting and lets running ones finish).

## Later
**Performance.** A persistent collection cache, `--lf`/`--ff`,

**Reporting.** JUnit XML, `--report-json`, and GitHub annotations.

**Coverage.** Ensuring we play nicely with coverage and can produce suitable reports.


### Needs human review

**Migration.** Codegen that rewrites a pytest suite's fixture wiring to velox, with no hand edits.
This is the difference between velox being adoptable and being greenfield-only.

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
[docs/migration-problem-statement.md](docs/migration-problem-statement.md) §4.2 measures as the
largest single source of hand edits in a migrated suite.

## Not planned

A plugin and hook system — dependency injection is the extension point, and zero hook dispatch on
the hot path is a design invariant. pytest syntax compatibility. `unittest`/`doctest`/`nose`
collection. Multi-process execution; `@velox.isolated` is the only subprocess path, though results
are serializable so the door stays open. First-class Windows support.
