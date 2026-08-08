# 04 — Dependency Injection: graph, scopes, lifecycle

*The honest framing from R§4: explicit injection deletes the **registry** — conftest visibility,
proximity overrides, autouse name walks, `FixtureLookupError` — about 600 of `fixtures.py`'s 2598
LOC, and most of the conceptual complexity users hate. It does **not** delete the hard ~700 LOC:
scope-keyed caching with invalidation, teardown inversion, and async lifecycle. Any design needs
those, velox will reinvent them, so they are specified here in detail.*

---

## 1. The graph

A fixture is a callable plus metadata. Its dependencies are the parameters whose default is
`Depends(...)`. The dependency graph is therefore **the literal object graph of referenced callables** —
there is nothing to resolve by name, and building it is a recursive walk over `__defaults__` with a
memo dict.

```python
@dataclass(frozen=True, slots=True)
class Fixture:
    fn: Callable
    scope: Scope                       # FUNCTION | MODULE | SESSION
    kind: Kind                         # SYNC | ASYNC | SYNC_GEN | ASYNC_GEN
    exclusive: frozenset[str]          # own tokens only
    deps: Mapping[str, Fixture | Const]
    name: str
```

Per test, collection produces a **`ResolutionPlan`**: the topologically sorted list of fixtures
needed, each with its scope and cache key, plus the transitive `exclusive` set. The plan is computed
once per test *function* and shared across parametrized callspecs. At run time, executing a plan is
a loop over a precomputed list — no graph walking, no signature inspection, nothing that touches
`inspect` (I5).

Cycles are detected during plan construction and reported with the full cycle path.

## 2. Static validation (before anything runs)

This is the structural upgrade explicit DI buys: pytest can only discover these lazily, one failing
test at a time (R§4). velox validates the whole graph once, at startup, and exits `4` with all
errors listed rather than the first.

| Check | Error |
|---|---|
| Scope compatibility | A `session` fixture depending on a `function` fixture is an error, not a runtime surprise. Ordering: `SESSION > MODULE > FUNCTION`; a fixture may depend only on equal-or-wider scopes. |
| Cycles | Full path reported. |
| `Depends()` on a non-fixture | Names the parameter and what it got. |
| Missing injection | A parameter with no default, not supplied by `@parametrize`, and not `self`. |
| Generator arity | A fixture generator that can yield more than once is not statically detectable in general; instead the *runtime* raises if a second yield occurs, with the fixture name. |
| Exclusive-token sanity | An `exclusive` token declared by a `function`-scoped fixture is fine; declared by a `session`-scoped fixture it means every dependent test is mutually exclusive — warn if that set is >50% of the suite, because it silently serializes the run. |

## 3. Scopes

| Scope | Instance keyed by | Torn down when |
|---|---|---|
| `function` | `(fixture, test.id, param_key)` | The test finishes (refcount → 0). |
| `module` | `(fixture, module_path, param_key)` | Refcount → 0: the last in-flight test from that module finishes. |
| `session` | `(fixture, param_key)` | End of the run (or refcount → 0 with `--eager-teardown` †). |

**Module scope under concurrency is a real semantic change and must be documented.** In pytest,
"module scope" means "alive for the contiguous run of that module's tests". Under concurrency it
means "alive while at least one test from that module is in flight" — which, given the scheduler
dispatches in logical order (= grouped by file), is nearly the same thing in practice but is *not*
guaranteed: a slow test from file A still in flight while file B runs keeps A's module scope alive.
That's benign. The non-benign case is the reverse assumption — that module scope teardown *has*
happened before the next module starts. Tests relying on that were relying on sequencing, and the
docs must say so. (Q2: keep `module`, rename it `file`, or drop it.)

## 4. Instantiation: single-flight

`FixtureDef.cached_result` in pytest is an unguarded read-modify-write that survives only because
pytest is sequential (R§4). velox's replacement:

```python
async def get(self, key: CacheKey) -> Any:
    fut = self._futures.get(key)
    if fut is None:
        fut = self._futures[key] = loop.create_future()
        try:
            value = await self._construct(key)      # may await user code
        except BaseException as exc:
            fut.set_exception(exc); raise
        else:
            fut.set_result(value)
    self._refcounts[key] += 1
    return await fut
```

- **First requester constructs, everyone else awaits.** The dict check and future creation happen in
  one synchronous step, so no lock is needed on the loop — but the code must not `await` between
  the `get` and the `set`, and that invariant is worth a comment and a test.
- **Exception caching is load-bearing.** A broken session-scoped DB fixture must produce *one*
  comprehensible error, not 400 identical ones (R§4). The future holds the exception; every
  dependent test fails with `error` outcome and a message that names the fixture and points at the
  single root traceback, which is printed once.
- **Param keys** are part of the cache key from day one, even though parametrized fixtures are
  roadmap ([01](01-public-api.md) §10) — retrofitting a cache key is exactly the kind of change this
  spec exists to avoid.

## 5. Teardown: refcount, not a stack

pytest's `SetupState` is a stack encoding "a fixture's lifetime is a contiguous run of adjacent
tests", which is **false under concurrency** (R§4). velox:

- Every scope instance has a refcount, incremented on acquisition by a test and decremented when
  that test's teardown phase completes.
- **A scope instance tears down when its refcount reaches zero** — except `session`, which tears
  down at end of run (an `--eager-teardown` mode that also refcounts session scope is roadmap; it
  would let a suite release its DB engine early, at the cost of possibly reconstructing it).
- **Teardown inversion** is preserved: dependents unwind before dependencies. pytest achieves this
  by registering a fixture's finalizer on each of its dependencies (R§4); velox achieves it
  structurally, by driving each scope instance through an `AsyncExitStack` and by the refcount
  ordering — a dependency's refcount cannot reach zero before its dependents' do, because a
  dependent holds a reference for its whole life.
- **Errors during teardown are collected into an `ExceptionGroup`** and reported against the test
  (or, for wider scopes, against the run, in a dedicated "teardown errors" section). A teardown
  failure after a passing call phase must surface — the test's outcome becomes `error`
  ([05](05-execution-model.md)).
- **Teardown at interruption** runs under `asyncio.shield` with a hard timeout. Leaked containers
  and schemas after Ctrl-C are a terrible first impression ([11](11-runtime-safety.md)).

Sync fixtures and sync generators are wrapped: a sync generator's teardown runs in the executor if
it is not trivially fast? No — it runs inline. Blocking teardown is a blocking call like any other
and the watchdog will name it.

## 6. Async lifecycle

Yield-fixtures become async generators driven by an `AsyncExitStack` owned by the scope instance.
Sync fixtures are called directly (they're cheap and usually pure); sync *generators* are adapted
via `contextlib.contextmanager` semantics into the same stack. The stack for a `function` scope
lives in the test task's context; the stacks for `module`/`session` scopes live in the session store
and are entered by whichever task first requests them — which is fine, because unwinding is driven
by refcount, not by the constructing task's lifetime.

One consequence worth stating: **the constructing task may be cancelled while others await the
future.** If the first requester is cancelled (`--maxfail`, Ctrl-C, timeout) mid-construction, the
future must be failed with a distinguishable `FixtureConstructionCancelled` so waiters report
`interrupted` rather than an opaque `CancelledError`. Construction of wider-than-function scopes is
therefore shielded from the requesting test's own timeout.

## 7. Why this is the head-to-head win

xdist duplicates session fixtures per worker: `-n 4` means 4 schemas, 4 engines, and the documented
workaround is a `FileLock` plus a marker file. velox's single process makes **one engine + N
concurrent tests** correct by construction (R§4). This belongs in the README, and the benchmark
should show it: on the 5000-test suite, xdist `-n 4` was measured *slower* than serial, because 4×
startup + 4× collection + IPC exceeded the parallelism win.

The corollary users must internalize: **shared session state is now genuinely shared, concurrently.**
The documented default for databases is transactional isolation — SAVEPOINT-per-test rollback, or a
connection-per-test on the shared engine ([05](05-execution-model.md) §3). The docs need a
recipes page for the reference stack (async SQLAlchemy + FastAPI + httpx) because getting this right
once, in a copy-pasteable form, is worth more than any amount of prose.

## 8. MVP

Graph construction from `__defaults__`, `ResolutionPlan` per test function, static validation
(scope compatibility, cycles, bad `Depends()`, missing injections), three scopes, single-flight futures
with exception caching, refcount teardown with `ExceptionGroup` collection, `AsyncExitStack`-driven
async generators, shielded construction for wide scopes.

## 9. Roadmap

- Parametrized fixtures (`params=`) with per-param instances; the cache key already carries it.
- `class` scope.
- `--eager-teardown` (refcount session scope too).
- Per-node fixture override (`Fixture.with_()`) and deep overrides by dependency path — see
  [01](01-public-api.md) §10 for the caching-identity problem that deferred it.
- A `--graph` dump (DOT/JSON) of the resolution graph: cheap, and a genuinely nice explainability
  feature that pytest structurally cannot offer.
- Lazy/optional fixtures (`Depends(p, lazy=True)` yielding a factory) for expensive resources only some
  branches need — deferred because it reintroduces "the footprint isn't statically known", which
  scheduling depends on ([06](06-scheduling-and-determinism.md)). If added, a lazy fixture's
  exclusive tokens must still be admitted up front.

## 10. Open questions

- **Q2** — Keep `module` scope (with the concurrency caveat), rename it to `file`, or ship only
  `function` and `session` in v1? Dropping it is the most honest option and the easiest to add back;
  keeping it eases migration, since pytest suites use it heavily.
- **Q11** — Should a fixture that raises during setup mark dependent tests `error` (proposed, and
  distinct from `failed` in the outcome enum) or `skipped` with a reason? pytest reports setup
  errors as errors; matching that is probably right, but `skipped` would keep CI dashboards greener
  in the "one flaky container" case, which is arguably a bug not a feature.
