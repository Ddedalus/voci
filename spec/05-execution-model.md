# 05 — Execution Model

*One process, one long-lived loop, tests as concurrent tasks. The measured target this replaces:
pytest's ~600 µs/test of pure protocol overhead — three pluggy multicalls through 52 hooks, three
report objects, capture suspend/resume — which is ~3.4× the entire collection cost of the 5000-test
benchmark, with real fixture work only ~13% of it (R§1).*

---

## 1. The loop

`asyncio.Runner(loop_factory=uvloop.new_event_loop if available else None)`, created once at
startup, owned by the runner, alive for the whole session. Tests are tasks bounded by a semaphore
(`--concurrency=N`, default 16).

`N ≫ cores` is deliberate and is the difference from xdist/jest: the workload is I/O-bound, so the
limit is the downstream service's tolerance (DB connections, container throughput), not CPU count.
The default is a fixed number rather than `cpu_count()`-derived because deriving it from cores would
be wrong for the intended workload and would make wall-clock non-reproducible across machines.

**Fresh-loop-per-test is not on the table.** Concurrency requires a shared loop by definition, and
the shared loop is what *deletes* the entire pytest-asyncio failure cluster: "attached to a different
loop", "event loop is closed" during teardown, `loop_scope` mismatch configuration. Every one of
those is downstream of "loop per test, but fixtures want to be broader" (R§5).

## 2. Per-test envelope

For each dispatched test, the runner creates one task whose body is:

```python
ctx = contextvars.Context()          # fresh, empty of prior test state
# seeded with: capture sink, log sink, patch overrides map, assertion hooks, test_info
async def run():                     # executed via ctx.run(...)
    async with asyncio.TaskGroup() as tg:      # in a ContextVar; child tasks join here
        async with asyncio.timeout(budget):    # per-test timeout
            setup()    →    call()    →    teardown()
```

Four isolation mechanisms replace process isolation (R§5):

1. **`TaskGroup` per test**, seeded into a ContextVar. Tests spawn background work through it. A
   non-empty group at test end means the test **fails**, naming the leaked coroutine's location.
   This converts "Task was destroyed but it is pending" shutdown noise into a named, actionable
   failure attributed to the test that caused it.
2. **Fresh `contextvars.Context` per test task** — per-test scoping that propagates to everything the
   test awaits or spawns, and to nothing else. The same mechanism powers capture ([09](09-capture-and-logging.md)),
   the assertion hooks ([07](07-assertions.md)), and — once tier (c) exists — patch overrides
   ([08](08-patching-and-isolation.md)).
3. **Transactional DB isolation as the documented default** — SAVEPOINT-per-test rollback, or a
   connection-per-test on the shared engine. This is a docs-and-recipes deliverable, not code, but
   it is load-bearing for the whole model and belongs in the reference stack guide.
4. **`@velox.isolated`** → run in a subprocess on a fresh loop, for tests touching signals, loop
   policy, `chdir`, C-level patching, or other process globals. The documented answer to "my test is
   weird", instead of degrading everyone else's model.

## 3. Phases

Keep pytest's setup/call/teardown as **internal** phases — a teardown failure after a passing call
must surface, and per-phase captured output is free — but emit **one aggregated `TestResult` per
test** (I3). pytest emits three reports per test and every downstream consumer re-aggregates them by
nodeid; there is no reason to inherit that.

| Phase | Contents | On exception |
|---|---|---|
| setup | Acquire the resolution plan's fixtures in topological order, evaluate `skipif`, honor `skip` | `error` (or `skipped` for a skip) ; call is not run; acquired fixtures are released |
| call | Await/execute the test function | `failed` (or `xfailed`/`xpassed` per the mark) |
| teardown | Release refcounts, unwind the function-scope `AsyncExitStack`, check the TaskGroup, check for un-awaited coroutine warnings | `error`, even if call passed; multiple errors → `ExceptionGroup` |

Timing is recorded per phase (`setup_s`, `call_s`, `teardown_s`) plus wall-clock start/end offsets
from session start — the latter is what makes the concurrency visualization (`--report-json` → a
Gantt view) possible later.

## 4. Outcomes

A real enum, not pytest's `hasattr(report, "wasxfail")` hack (checked in five files, purely for
plugin back-compat velox doesn't owe) — R§5:

```
passed | failed | error | skipped | xfailed | xpassed | interrupted | timeout
```

| Outcome | Meaning | Exit-code contribution |
|---|---|---|
| `passed` | Call succeeded | 0 |
| `failed` | Call raised (incl. `AssertionError`) | 1 |
| `error` | Setup or teardown raised | 1 |
| `skipped` | `skip`/`skipif`/runtime `velox.skip()` | 0 |
| `xfailed` | Expected failure, and it failed | 0 |
| `xpassed` | Expected failure, but passed. Fails the run under `strict=True` | 0 / 1 |
| `interrupted` | Cancelled by `--maxfail`, Ctrl-C, or a session-level abort | 2 |
| `timeout` | Exceeded the per-test budget | 1 |

`timeout` is a distinct outcome rather than a flavor of `failed` because it is near-free with tasks
(`asyncio.timeout`) and because the remedy is completely different — it pairs with the watchdog for
the sync-blocking case ([11](11-runtime-safety.md)) — R§8.7.

## 5. Silent-pass eliminations

Three cases where pytest can report green on a broken test. Each is a failure in velox (I8):

1. **Un-awaited coroutine.** pytest's only async extension point is a `trylast` hook that plugins
   race to win; when the ordering goes wrong the coroutine is never awaited and the test *passes*
   silently (R§5). velox: a `RuntimeWarning: coroutine ... was never awaited` raised during a test
   fails that test, attributed via the capture ContextVar.
2. **Non-`None` return from a test.** Error, with the returned value's repr.
3. **Leaked task in the TaskGroup.** Failure, with the leaked coroutine's definition site.

## 6. Cancellation semantics

Cancellation arrives from three sources: per-test `asyncio.timeout`, `--maxfail`, and SIGINT. In all
three:

- The test task is cancelled. Its `CancelledError` is caught at the envelope boundary and converted
  to `timeout` or `interrupted` — never propagated as a test failure with an asyncio traceback.
- **Teardown still runs**, under `asyncio.shield` with its own hard timeout (default 10 s, config
  `teardown_grace`). A teardown that exceeds the grace is reported as a teardown error naming the
  fixture, and the run continues — but resources may have leaked, and the reporter says so.
- Wide-scope fixture *construction* is shielded from the requesting test's timeout ([04](04-dependency-injection.md) §6),
  so one slow test doesn't poison a session engine for everyone.

## 7. Sync tests and sync code

Sync test functions and sync fixtures run in the loop's **context-propagating default executor**
([09](09-capture-and-logging.md) §3), so their output and patch overrides are still attributed
correctly. They occupy a concurrency slot for their whole duration. Blocking calls inside *async*
tests are the dangerous case and are the watchdog's entire reason for existing.

## 8. Result record

```python
@dataclass(frozen=True, slots=True)
class TestResult:
    id: str
    index: int                       # logical order — the sort key for all output (I2)
    outcome: Outcome
    duration: PhaseTimings
    started_at: float; ended_at: float
    failure: FailureRepr | None      # serializable; built lazily, only on failure
    captured_out: str; captured_err: str
    log_records: list[LogRecordRepr]
    warnings: list[WarningRepr]
    tags: frozenset[str]
    worker_slot: int
```

JSON-round-trippable from day one (I4) — it is what makes `--isolated`, `--report-json`, a future
multi-process mode, and velox's own tests possible. Retrofitting this forced xdist's `Repr*` tree
into pytest core, the cautionary tale (R§8.8).

## 9. MVP

Shared loop with optional uvloop; semaphore-bounded tasks; per-test context + TaskGroup + timeout;
three phases with one aggregated result; the full outcome enum; the three silent-pass eliminations;
cancellation with shielded teardown; sync tests via the executor; the serializable `TestResult`.

## 10. Roadmap

- `@velox.isolated` subprocess tier: fork/spawn a worker, run one test, ship back a `TestResult` as
  JSON. Cheap *because* of I4.
- `--isolated-all` for diagnosing a suite.
- Concurrency visualization from the timing data (Gantt in the JSON report / an HTML view).
- Adaptive concurrency (back off when the loop's mean callback latency degrades) — attractive, but
  in tension with reproducible wall-clock; would ship off by default.
- Multi-process mode combining N processes × M tasks, for suites that are partly CPU-bound. Only
  after the single-process story is proven; the report format already permits it.

## 11. Open questions

- **Q12** — Should the per-test timeout default be finite (300 s, proposed) or off? Finite means a
  hung suite always terminates with a diagnosable report, which matters more in CI than the risk of
  killing a legitimately slow integration test. Off matches pytest.
- **Q13** — On `--maxfail`, should in-flight tests be cancelled (proposed, reported `interrupted`)
  or allowed to finish? Cancelling is faster and matches "stop now"; letting them finish yields more
  information per run. Proposed behavior is documented explicitly either way, because a test
  reported `interrupted` that would have failed is a real information loss.
