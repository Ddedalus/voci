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
selection by `path.py::test_name` id, `-k` and `@velox.tag` with `-m` · `-x`/`--maxfail`
and Ctrl-C, both cancelling what is in flight · `--serial`, `--collect-only`, `-v`/`-q` and
`--durations` · assertion introspection with comparison
diffs · stdout/stderr/logging capture and `tmp_path` · the event-loop watchdog and the failure a
test earns for returning a value or dropping a coroutine un-awaited · the reporter ·
`[tool.velox]` config · `velox.fastapi` per-test dependency overrides.

## Next
**Codegen**: velox-migrate, plans:
High-level in `plans/migration-tool-plan.md`
Currently chewing through: `plans/migration-matrix-improvements.md`

Next: smoke-test on real open-source suites:
1. Marshmallow
2. httpx2 (successor to https, active development)


### Urgent performance fixes
To make test suite viable to run repeatedly.

**Resolve QualifiedNameProvider/ParentNodeProvider once per file, not once per rule.** Build one MetadataWrapper over the original module, and pass its resolved caches into each subsequent wrapper.visit() via libcst's cache= param on MetadataWrapper.__init__ — since unsafe_skip_copy=True already preserves node identity, untouched nodes hit the cache and only newly-synthesized nodes need fresh resolution. This should collapse the dominant cost from ~19 full-tree scope passes down to ~1 per file. Given metadata resolution is ~70% of profiled convert.run() time, this is plausibly a 3-5x wall-clock win on its own.

**Skip rules that can't match.** Several rules only care about specific decorator/call shapes; a cheap pre-check (e.g. "does this file even import pytest/contain a @pytest.mark...") could skip the MetadataWrapper construction entirely for files with nothing to do. Secondary to #1, not needed if #1 lands.
Memoize conversion_of() in the test file (it's pure — no disk writes) per (suite, version) with functools.cache, so the ~10x-per-suite redundant recomputation in test_convert.py collapses to one real call per suite/version pair. converted() still needs its own tmp_path copy per test (since tests mutate the tree and run subprocesses against it), but it can reuse a cached Conversion object rather than recomputing one.

## docs
High-level plan in `plans/docsite-plan.md`

## Code quality consolidation

Code review of velox so far, coverage gaps, clean CI, drop fat, identify duplication etc.


## Later

## Starvation-aware scheduling
`exclusive=`/`@velox.solo` admission has no fairness guarantee: a
steady stream of ordinary tests can keep a waiting solo or exclusive-resource test from ever
seeing an opening.

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
[plans/migration-problem-statement.md](plans/migration-problem-statement.md) §4.2 measures as the
largest single source of hand edits in a migrated suite.

## Not planned

A plugin and hook system — dependency injection is the extension point, and zero hook dispatch on
the hot path is a design invariant. pytest syntax compatibility. `unittest`/`doctest`/`nose`
collection. Multi-process execution; `@velox.isolated` is the only subprocess path, though results
are serializable so the door stays open. First-class Windows support.
