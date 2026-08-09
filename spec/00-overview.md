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
| DI-override mocking; `velox.fastapi` ContextVar-layered `dependency_overrides`/`app.state`; stock `unittest.mock` detected and scheduled **solo** | General-purpose task-local routing `velox.patch` (tier c) |
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
| `velox.fastapi` layered client | 120 | Layered overrides + state, install-once, escalation ([08](08-patching-and-isolation.md) §3.1) |
| **Fresh total** | **~3.4k** | |
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

Four things the examples surfaced. The second and first are **resolved**; they stay here with
their resolutions because the reasoning is load-bearing for the reference stack.

### Fixture import path — RESOLVED (2026-08-09)
1. Nothing in the spec makes tests/fixtures.py importable. This is the real one. spec/02 §5 says velox never touches sys.path; spec/03 imports test modules under generated velox_tests.* names. Neither makes the test tree a package, so from tests.fixtures import api_client — the only way to share a fixture, since there's no conftest — cannot resolve. The examples assume velox prepends the rootdir to sys.path once at startup: one predictable insertion, not pytest's per-conftest-directory games. Whatever the answer, it needs writing down, because it's load-bearing for the no-conftest decision.

A: Editing sys.path is trivial. What matters is that we need a convention that will work with all the mainstream tooling: ruff, pyright/pyrefly, VSCode language server etc. There is no use of imports that show in red in the IDE or require intricate configuration for every new dev tool.

The resolution takes the steer literally and stops there: `cli.main` prepends `rootdir` (the same
`rootdir` `_config.resolve` already computes, spec/02 §3) to `sys.path[0]` exactly once, at the
same point `[tool.velox] env` is applied — before the first test module import, restored in a
`finally` so repeated in-process `main()` calls (this repo's own suite does that) never accumulate
duplicate entries. Nothing else changes: `_collect.py` keeps importing every test file under its
unique, synthetic `velox_tests.<relpath>` name via `importlib.util.spec_from_file_location`
(spec/03 §3), unmodified. That one insertion is enough because every import the examples actually
need is a plain **absolute** import rooted at `rootdir` — `from relay.cache import FakeClock`
(`examples/02-async-library`), `from tests.fixtures import api_client` and `from app.db import
get_session` (`examples/01-fastapi-crud`) — and those resolve via ordinary PEP 420 implicit
namespace-package lookup the instant `rootdir` is on `sys.path`, no `__init__.py` anywhere
required. This is exactly the layout pyright/Pylance, ruff's import sorter, and VS Code's default
Python analysis already assume once a `pyproject.toml` sits at that directory, so it costs
adopters zero configuration — the review note's bar.

What it deliberately does **not** fix: a *relative* import between test modules, e.g.
`tests/assertion/test_explanations.py`'s `from .conftest import callequal`. That resolves against
the importing module's `__package__`, which under the synthetic-name scheme is `velox_tests.
assertion` — a name with no directory anywhere on disk — so no `sys.path` entry can ever satisfy
it (the failure names `velox_tests`, not `tests`; `sys.path` is never even consulted). Building
that back would mean synthesizing namespace-package stubs for every ancestor directory, i.e.
reintroducing the per-directory package-walking machinery spec/03 §3 explicitly deletes versus
pytest, to support a convention this resolution rejects outright: **relative imports between test
modules are unsupported.** Share code via an absolute import rooted at `rootdir` instead (`from
tests.assertion.conftest import callequal`) — the one `sys.path` insertion above already makes
that work. This doesn't block the M1 gate: `examples/01`/`examples/02` use only absolute imports;
`tests/assertion/`'s one relative import only surfaces when running velox's own suite under
velox, which M1-PLAN's own dogfood item already treats as not realistic before M3.
Spec updated: [02](02-cli-and-config.md) §5, [03](03-discovery-and-collection.md) §3.

### FastAPI testing — RESOLVED (2026-08-07)
2. `app.dependency_overrides` and `app.state` are per-app-instance mutable dicts, so the docs-blessed idiom — mutate a module-level singleton's overrides and reset in teardown — is a process-global write that concurrent tests clobber; the `create_app(settings)` factory that dodges it is adoption-hostile, because real FastAPI code is singleton-shaped and no team rewrites production wiring to adopt a test runner. The resolution keeps the singleton and moves the *view*: routes bake in only a pointer to the app (`dependency_overrides_provider`, captured at route-decoration time) and read the override dynamically on every request via `getattr(provider, "dependency_overrides", {}).get(call, call)`, so replacing that attribute once with a ContextVar-layered `Mapping` proxy makes overrides per-test while the app object stays shared and untouched. A new MVP module, `velox.fastapi`, installs the proxy once per app object and scopes each `client()` block's mappings to a layer — reads consult layer then base, writes inside a layer stay in the layer, nested clients stack inner-wins — with `app.state` layered the same way and a loud I6 escalation if the proxy is ever replaced out from under velox. This is tier (c)'s ContextVar-routing insight applied to exactly one well-behaved surface (~120 LOC, no general patch machinery); general-purpose `velox.patch` remains deferred. It rests on a handful of upstream facts, which velox pins with assumption tests (`tests/test_fastapi_layering.py`) so that an upstream change breaks velox's own suite rather than adopters' runs. Mechanism, limits, and rejected alternatives: [08](08-patching-and-isolation.md) §3.1; the surface and the canonical fixture: [01](01-public-api.md) §3.

### B008 false positives — `with_()` portion superseded (2026-08-08)
3. B008 fires on every test in a velox suite. Depends(...) in a parameter default is a function call in an argument default. Fixable with extend-immutable-calls = ["velox.Depends"], which every example's pyproject.toml now carries — that line belongs in getting-started.

   The rest of this entry was about `with_()`'s ergonomics specifically: inline `with_(transport=fake)` has no qualified name to whitelist, so the examples bound derived fixtures to module-level names instead. `with_()` itself is now deferred to roadmap ([01](01-public-api.md) §10) for a different, more fundamental reason — a caching-identity problem — but this B008 cost is recorded there too, as a second, independent argument against shipping the surface as first drafted.

A: I am confused here. I know pytest people would build fixtures like that, but I never saw a FastAPI dep built this way. With that note, the FastAPI DI has an annoying limitation that dep functions cannot be parametrized. I'd rather have `value: T = Depends(dep_factory, param=...)` than have to call the factories this way.

   That instinct is most of why `with_()` ended up deferred rather than shipped: today's replacement is exactly the plain-function form gestured at above — a sibling `@velox.fixture()` that wires in the replacement dependency directly, no derivation method involved:
   ```python
   @velox.fixture()
   def flaky_relay(transport: FakeTransport = Depends(flaky_transport)) -> Relay:
       return Relay(transport, retries=3, base_delay=0.001)
   ```
   Being an ordinary function definition rather than a call expression sitting in an argument default, it never trips B008 in the first place.

### tmp factory type
4. velox.tmp_path_factory has no named type. Used here as velox.TmpPathFactory with .mktemp(name).