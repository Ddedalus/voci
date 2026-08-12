# Roadmap

velox is pre-v0.1. This page is the single honest list of what isn't finished — if a feature isn't
mentioned in [README.md](README.md) or [docs/](docs/), assume it's here.

## Working today

Discovery and collection · explicit dependency injection with `call`/`function`/`module`/`session`
scopes, single-flight construction and inverted teardown · concurrent execution under a semaphore
with per-test timeouts, overridable with `@velox.timeout(...)` · `exclusive=` on a fixture and
`@velox.solo`, admission-controlled against everything else running · `@velox.isolated`'s
per-test subprocess tier · `skip`/`skipif`/`xfail` ·
`@velox.parametrize`, including stacked decorators · `@velox.tag` selection with `-m` ·
assertion introspection with comparison diffs · stdout/stderr/logging capture and `tmp_path` ·
the reporter · `[tool.velox]` config · `velox.fastapi` per-test dependency overrides.

## Next

### 01-fastapi-crud example improvements
Audience of the example is new user. Narrator of the example is velox creator.
Remind yourself good quality narrative documentation like FastAPI's - it ought to be friendly and pleasant to read.

1. Apply our docstring best practice to the example. Remove all hectoring and tirrades about what not. An example speaks by the code primarily + simple comments like 'note that we took care of making app.dependency_overrides concurrency-safe for you - it just works' 
2. The entire SQLAlchemy setup is too complicated - it should be hidden in a helper file/class and just imported with a brief comment.
3. I understand SQLite is used for simplicity, which causes some issues; again, hide them in helper file and just add a brief comment stating this is a simplification


**Starvation-aware scheduling.** `exclusive=`/`@velox.solo` admission has no fairness guarantee: a
steady stream of ordinary tests can keep a waiting solo or exclusive-resource test from ever
seeing an opening.

**Runtime safety.** A loop-starvation watchdog that names the blocking call instead of letting the
suite mysteriously stall; failing a test that returns a value or leaves a coroutine un-awaited;
Ctrl-C and `--maxfail` cancellation with time-boxed teardown.

**Collection.** `class Test*` as pure namespacing, and a diagnostic for test shapes that currently
collect as zero tests rather than as an error.

**CLI.** `-k` selection, `path.py::test_name` ids, `-v`/`-q`, `-x`, `--collect-only`,
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
