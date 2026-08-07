# velox — Specification: Overview

Status: human reviewed.

## 1. What velox is

velox is a test runner for **fully-async Python codebases** (FastAPI + async SQLAlchemy is the
reference shape). It runs a whole suite in **one process on one long-lived event loop**, with tests
as **concurrent asyncio tasks**, and it replaces pytest's implicit name-based fixture system with
**explicit dependency injection** following FastAPI patterns. velox leverages the **existing pytest assertion introspection** to provide familiar error messages. velox also works with `coverage` out of the box.

This allows some important wins over pytest and xdist:

1. **Concurrency.** Testing an ASGI app against a database with transaction isolation delivers load test performance rather than sequential pytest grind.
2. **Correct-by-construction shared resources.** No xdist's session-scoped fixture nonsense.
3. **Deterministic output.** Collection order and physical execution order are separated, so runs are byte-identical regardless of task timing.


The cost of admission is quite simple:
1. velox is not pytest
2. velox has no plugin ecosystem
3. you must rewrite your fixtures wiring, with the help of a migration tool
4. python 3.13+

## 2. Non-goals

| Not building | Why |
|---|---|
| A plugin/hook system | Zero hook dispatch on the hot path is a design invariant - use DI instead. |
| pytest syntax compatibility | Implicit name-based fixture resolution is unsalvageable. |
| unittest / doctest / nose collection | Out of scope by design. |
| Multi-process execution in v1 | `--isolated` (per-test subprocess) is the only subprocess path. Report serializability keeps a future multi-process mode open (§6, I4). |
| Windows support | Linux/macOS are the supported targets in v1; Windows is best-effort (fd/signal/`os._exit` paths differ). |

## 4. Architecture at a glance

```
                       ┌──────────────────────────────────────────────┐
  argv ──► config ────►│ discovery: one os.scandir walk + name filter  │
  (one parse)          │ importlib import (rewriting meta-path hook)   │
                       │ → flat list[TestRecord]  (no node tree)       │
                       └───────────────────┬──────────────────────────┘
                                           │ logical order = (path, lineno, param idx)
                                           │ static DI graph + scope validation
                                           ▼
                       ┌──────────────────────────────────────────────┐
                       │ scheduler: greedy over logical order,        │
                       │ semaphore(N) + all-or-nothing exclusive-set  │
                       │ admission + solo lock                        │
                       └───────────────────┬──────────────────────────┘
                                           │ dispatch
                       ┌───────────────────▼──────────────────────────┐
      one asyncio      │  per test:  fresh contextvars.Context        │
      loop, one        │             TaskGroup + asyncio.timeout      │
      process          │             setup → call → teardown          │
                       │  scope store: single-flight Futures,         │
                       │               refcounted teardown            │
                       └───────────────────┬──────────────────────────┘
                                           │ TestResult (one per test, serializable)
                       ┌───────────────────▼──────────────────────────┐
                       │ reporter: per-file scrollback blocks,        │
                       │ live footer, failures in LOGICAL order       │
                       │ + JUnit XML / JSON / GH annotations          │
                       └──────────────────────────────────────────────┘
      ┌──────────────────────────────────────────────────────────────┐
      │ cross-cutting, all ContextVar-routed: capture sink, log sink, │
      │ assertion hooks, patch overrides.  Watchdog thread outside.   │
      └──────────────────────────────────────────────────────────────┘
```

## 5. Component map

| # | Component | File |
|---|---|---|
| 1 | Public API — the surface a test author writes against | [01-public-api.md](01-public-api.md) |
| 2 | CLI & configuration, exit codes | [02-cli-and-config.md](02-cli-and-config.md) |
| 3 | Discovery, import, collection, the collection cache | [03-discovery-and-collection.md](03-discovery-and-collection.md) |
| 4 | Dependency injection: graph, scopes, lifecycle, teardown | [04-dependency-injection.md](04-dependency-injection.md) |
| 5 | Execution model: loop, tasks, phases, outcomes | [05-execution-model.md](05-execution-model.md) |
| 6 | Scheduling, exclusive resources, determinism | [06-scheduling-and-determinism.md](06-scheduling-and-determinism.md) |
| 7 | Assertion introspection (vendored rewriter) | [07-assertions.md](07-assertions.md) |
| 8 | Patching, mocking, isolation tiers | [08-patching-and-isolation.md](08-patching-and-isolation.md) |
| 9 | Capture: stdout/stderr, logging, tmp_path | [09-capture-and-logging.md](09-capture-and-logging.md) |
| 10 | Tracebacks, reporter, machine-readable output | [10-reporting.md](10-reporting.md) |
| 11 | Runtime safety: watchdog, signals, warnings, unraisables, coverage | [11-runtime-safety.md](11-runtime-safety.md) |
| 12 | pytest → velox migration codegen | [12-migration.md](12-migration.md) |

## 6. Cross-cutting invariants

These are binding on every component. A design that violates one needs this document changed first.

- **I1 — No module-global mutable state.** Anything per-test lives in a `ContextVar`; anything
  shared is owned by an explicit session object passed down.
- **I2 — Logical order governs all output.** Failure details, short summary, JUnit testcases, JSON
  records. Two runs of the same test set with the same seed produce byte-identical output modulo
  timings and paths (R§6).
- **I3 — One aggregated result per test.** setup/call/teardown stay as internal phases; exactly one
  `TestResult` reaches the reporter. Pytest emits three and every consumer re-aggregates (R§5).
- **I4 — Results are JSON-round-trippable from day one.** Buys `--isolated`, `--report-json`,
  future multi-process, and testability.
- **I5 — Zero dispatch overhead on the hot path.** No hook system, no per-test object trees, no
  eager failure representations. Failure reprs are built only on failure.
- **I6 — Escalate, never silently degrade.** Where a mechanism cannot be made concurrency-safe
  (raw global patching, fd capture, per-test warning filters), velox escalates the test to a
  stricter tier (solo → isolated) or warns loudly and names the cause. It never pretends.
- **I7 — Cold start is a first-class budget.** < 50 ms from process start to first test dispatched,
  CI-checked. No entry-point scanning, one argv parse, lazy imports, guaranteed-writable rewrite
  cache (R§8.5, R§2).
- **I8 — Silent passes are bugs.** An un-awaited coroutine, a leaked task, a selector that matched
  nothing — each is a failure or a distinct exit code, never a green run (R§5).

## 7. MVP scope

The MVP is **v0.1: a runner good enough to run velox's own test suite and one real FastAPI service
suite**, migrated by hand. Everything in the "MVP" column is required for that; everything else is
sequenced in the roadmap sections of the component specs.

| In MVP | Deferred |
|---|---|
| Discovery, importlib-only import, flat records, full collection before dispatch | Persistent collection cache, `--lf`/`--ff` |
| Explicit DI, scopes `function`/`session`, async yield-fixtures | `module` scope, parametrized fixtures, lazy/optional fixtures |
| Concurrent execution, semaphore, TaskGroup, per-test timeout | `--isolated` subprocess tier |
| Solo tier | Exclusive-resource admission, aging/starvation tuning beyond the basic rule, footprint-aware bin packing |
| Vendored assertion rewriter + comparison-diff engine + PEP 657 floor | `--no-rewrite` re-evaluation mode |
| Capture router (stdout/stderr/logging), `tmp_path` | `caplog` filtering API surface beyond records, `-s` line prefixing |
| Reporter: tty blocks, non-tty mode, short summary | live footer, `--durations`, JUnit XML, `--report-json`, GH annotations, `--stream-failures` |
| Marks: skip/skipif | xfail/parametrize/tags, `-k`/`-m` |
| Ctrl-C choreography, un-awaited-coroutine failure |  Loop-starvation watchdog, unraisable attribution polish, warnings-in-parallel story |
| DI-override mocking; stock `unittest.mock` detected and scheduled **solo** | Task-local routing `velox.patch` (tier c) |
| — | Migration codegen (separate deliverable, starts after v0.1 API freeze) |

## 8. Milestones

- **M0 — Walking skeleton (target: smallest useful thing).** Discover → import → run one async test
  on the shared loop → print pass/fail → correct exit code. No fixtures, no concurrency. Proves the
  import path and the loop ownership.
- **M1 — Core runner.** DI + scopes + teardown; concurrency + semaphore; vendored assertions;
  capture; the reporter. This is v0.1 and the bulk of the work.
- **M2 — Safety and ergonomics.** Watchdog, signals, timeouts, exclusive/solo scheduling, patching
  tiers, `--isolated`, JUnit/JSON, collection cache.
- **M3 — Adoption.** Migration codegen, `coverage.py` verification in CI, docs, the benchmark story
  (velox vs pytest vs `xdist -n 4` on a real suite, published and reproducible).

**Gate on M1:** velox runs its own suite. **Gate on M2:** velox runs the reference FastAPI suite
green, with wall-clock ≥ 3× faster than serial pytest on the same machine. **Gate on M3:** the
codegen migrates that suite from pytest with no hand edits to fixture wiring.

## 9. Sizing budget

From R§9, adjusted into a per-component budget. These are estimates used to detect scope creep, not
targets to hit.

| Area | LOC (fresh) | Notes |
|---|---|---|
| Discovery + records | 300 | |
| DI resolver + static validator | 400 | |
| Scope lifecycle, refcount, single-flight | 500 | The genuinely hard part (R§4) |
| Scheduler | 300 | |
| Capture router + log handler | 200 | |
| Watchdog + signals | 200 | |
| Reporter | 600 | |
| JUnit/JSON emitters | 250 | |
| CLI/config (single parse) | 200 | |
| Patch router (tier c) | 300 | Not in MVP — see [08](08-patching-and-isolation.md) |
| **Fresh total** | **~3.3k** | |
| Vendored rewriter + explanation engine | ~2.4k | Plus upstream tests |

## 10. Principal risks

1. **Blocking calls in user code destroy the value proposition.** One sync DB driver freezes the
   whole suite. Mitigation is the watchdog and duration tracking.
2. **Migration cost exceeds the speed win.** Mitigated by codegen; if codegen slips, velox is a
   greenfield-only tool. This is the single biggest adoption risk and M3 exists for it.
3. **Cold-start regression in CI containers.** If the rewrite pyc cache is unwritable velox silently
   pays 4.6× per run. Must fall back to no-rewrite mode explicitly and loudly (R§2).
4. **Concurrency exposes latent test-order and shared-state bugs in adopters' suites.** Real bugs,
   but they will be reported as velox bugs. Needs a documented triage ladder and good failure
   messages (`--concurrency=1` as the first debugging step).
5. **Vendored-rewriter drift.** We fork `rewrite.py`; upstream keeps moving. Pin a known-good pytest
   commit, record it, and re-vendor deliberately rather than tracking.

## 11. Outstanding decisions

Four things the examples surfaced:

### Fixture import path
1. Nothing in the spec makes tests/fixtures.py importable. This is the real one. spec/02 §5 says velox never touches sys.path; spec/03 imports test modules under generated velox_tests.* names. Neither makes the test tree a package, so from tests.fixtures import api_client — the only way to share a fixture, since there's no conftest — cannot resolve. The examples assume velox prepends the rootdir to sys.path once at startup: one predictable insertion, not pytest's per-conftest-directory games. Whatever the answer, it needs writing down, because it's load-bearing for the no-conftest decision.

A: Editing sys.path is trivial. What matters is that we need a convention that will work with all the mainstream tooling: ruff, pyright/pyrefly, VSCode language server etc. There is no use of imports that show in red in the IDE or require intricate configuration for every new dev tool.

### FastAPI testing
2. spec/01 §3's own api_client snippet has the footgun spec/08 §3 warns about. It mutates a module-level app's dependency_overrides — per-instance state that concurrent tests would clobber. Example 01 uses a create_app(settings) factory; the spec snippet should probably follow, since it's the first code a reader sees.

A: this is a fundamental problem with FastAPI: the overrides are not well fleshed-out. I am concerned that constructing the app may be slow in real life (it builds all the route resolvers etc.) and that a lot of existing code asumes it's a global singleton. However, most app access in tests is via a test client, which people hand-roll based on the docs and their other requirements, so there is a natural surface for a factory. I would be open to some hacks with shallow/deep copy, since I believe the app itself is mostly immutable after construction - we should leverage that. FastAPI doesn't change very dynamically nowadays, so as long as we have good test coverage of our assumptions, this should be easy to maintain.

### B008 false positives
3. B008 fires on every test in a velox suite. Depends(...) in a parameter default is a function call in an argument default. Fixable with extend-immutable-calls = ["velox.Depends"], which every example's pyproject.toml now carries — that line belongs in getting-started. Inline with_() is worse: relay.with_(transport=fake) has no qualified name to whitelist, so the examples bind derived fixtures to module-level names. That reads better and is shareable, so it should just be the documented idiom.

A: I am confused here. I know pytest people would build fixtures like that, but I never saw a FastAPI dep built this way. With that note, the FastAPI DI has an annoying limitation that dep functions cannot be parametrized. I'd rather have `value: T = Depends(dep_factory, param=...)` than have to call the factories this way. In any case, I believe your example is resolved the FastAPI way with something like:
```python
def flaky_client() -> AsyncClient:
    yield relay.with_(transport=fake).client()
```

### tmp factory type
4. velox.tmp_path_factory has no named type. Used here as velox.TmpPathFactory with .mktemp(name).