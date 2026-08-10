# Roadmap

velox is pre-v0.1. This page is the single honest list of what isn't finished — if a feature isn't
mentioned in [README.md](README.md) or [docs/](docs/), assume it's here.

## Working today

Discovery and collection · explicit dependency injection with `call`/`function`/`module`/`session`
scopes, single-flight construction and inverted teardown · concurrent execution under a semaphore
with per-test timeouts · `skip`/`skipif` · assertion introspection with comparison diffs ·
stdout/stderr/logging capture and `tmp_path` · the reporter · `[tool.velox]` config ·
`velox.fastapi` per-test dependency overrides.

## Declared but not enforced

These marks exist in the API and are accepted today, but nothing acts on them yet. **A suite that
relies on them for safety will race.** Until they land, keep conflicting tests from running
concurrently by hand, or run with `--concurrency 1`.

- `exclusive=` on a fixture, and `@velox.solo` — no admission control and no suite-wide write
  lock, so a test that declares a shared resource still runs alongside everything else.
- `@velox.isolated` — the per-test subprocess tier. Runs in-process like any other test.
- `@velox.xfail` — an expected failure reports as `FAILED`, indistinguishable from a regression.
- `@velox.tag` — recorded, but there is no `-m` to select on it.
- `@velox.parametrize` — recorded, but not expanded at collection; a parametrized test currently
  fails collection because its extra parameter looks like a missing dependency.

Two more shapes are collected as *nothing at all*, with no error: a plain `def test_*` (collection
requires a coroutine function) and a method on a `class Test*`. Write tests as module-level
`async def` until both report properly.

## Next

**Scheduling.** Exclusive-resource admission (all-or-nothing over a test's whole footprint), the
solo write lock, and starvation-aware ordering. This is what makes the marks above real, and it is
the largest single piece of remaining work.

**Runtime safety.** A loop-starvation watchdog that names the blocking call instead of letting the
suite mysteriously stall; failing a test that returns a value or leaves a coroutine un-awaited;
Ctrl-C and `--maxfail` cancellation with time-boxed teardown.

**Collection.** `@velox.parametrize` expansion, `class Test*` as pure namespacing, and a diagnostic
for test shapes that currently collect as zero tests rather than as an error.

**CLI.** `-k`/`-m` selection, `path.py::test_name` ids, `-v`/`-q`, `-x`, `--collect-only`,
`--serial`, `--durations`.

**Mocking tiers.** Detecting stock `unittest.mock` patching and scheduling those tests solo, with
the cost reported in the run summary. Related: a `@mock.patch`-decorated test hides its real
signature, so its `Depends()` defaults are silently not injected — that needs fixing regardless.

**Reporting.** A live footer, JUnit XML, `--report-json`, and GitHub annotations.

## Later

**Migration.** Codegen that rewrites a pytest suite's fixture wiring to velox, with no hand edits.
This is the difference between velox being adoptable and being greenfield-only.

**Performance.** A persistent collection cache, `--lf`/`--ff`, and a published, reproducible
benchmark against pytest and `pytest-xdist` on a real suite.

**Injection ergonomics.** Parametrized fixtures, and lazy or optional dependencies.

## Not planned

A plugin and hook system — dependency injection is the extension point, and zero hook dispatch on
the hot path is a design invariant. pytest syntax compatibility. `unittest`/`doctest`/`nose`
collection. Multi-process execution; `@velox.isolated` is the only subprocess path, though results
are serializable so the door stays open. First-class Windows support.
