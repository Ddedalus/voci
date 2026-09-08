# trio support: implementation plan

Internal working document. Answers "what would it take to run trio tests under voci", with a
design for the cheap option and a costed dismissal of the expensive one.

voci's execution model is asyncio all the way down by deliberate choice
([spec/05](../spec/05-execution-model.md) §1): one `asyncio.Runner`, optionally on uvloop, alive
for the whole session, with tests as semaphore-bounded tasks. That decision is what deletes the
pytest-asyncio failure cluster, and this plan does not reopen it. The plan below runs trio tests
*beside* that loop rather than replacing it.

---

## 1. Decisions at a glance

| Question | Decision |
|---|---|
| Does the runner become backend-agnostic? | **No.** See §6 — a full anyio port is 4–6 weeks and costs uvloop and the cancellation semantics. |
| Where does a trio test run? | On its own `trio.run`, inside the existing worker-thread pool — the same path sync `def` tests already take |
| How is a trio test identified? | An explicit `@voci.trio` mark. A trio coroutine function is indistinguishable from an asyncio one by `inspect`, so voci has to be told |
| Is trio a voci dependency? | No. Optional extra `voci[trio]`, imported lazily; using the mark without trio installed is a collection error |
| Per-test timeout | Honored, via `trio.move_on_after` inside the wrapper — trio-side, so it actually stops the test |
| `--maxfail` / Ctrl-C cancellation | Best-effort in step 5 via the run's `TrioToken`; without it a trio test in flight behaves like a sync test (abandoned, named by `stuck_calls`) |
| trio-native async fixtures | Not supported. Fixtures are constructed on the asyncio loop, as today |

---

## 2. Why a thread, not the loop

`trio.run` needs to own its thread. The one place voci already hands a test its own thread with
full context propagation is the sync-test branch of the call phase
([run.py:291](../voci/_run/run.py#L291)), dispatching into
`_capture.ContextPropagatingExecutor` ([capture.py:356](../voci/_builtins/capture.py#L356)).
That executor copies the calling task's `contextvars.Context` at submit time, which is exactly
what every voci subsystem keyed off a ContextVar needs: the capture sink, the log routing
handler (already documented as running on worker threads,
[capture.py:180](../voci/_builtins/capture.py#L180)), the assertion hooks, `test_info`. A trio
test dispatched down this path gets correct output attribution for free.

The pool is already sized `max(concurrency, default_max_workers)`
([run.py:1006](../voci/_run/run.py#L1006)), so a trio test never waits for a thread while its own
timeout budget burns. It occupies one concurrency slot and one worker thread for its duration,
same as a sync test.

## 3. Build order

**Step 1 — the mark.** `voci.trio`, a bare decorator folding `trio: bool = False` into `Marks`
([_marks.py:98](../voci/_marks.py#L98)), alongside `solo` and `isolated`. Exported from
`voci/__init__.py`. Collection rejects the combination `@voci.trio` on a non-`async def`
function, and rejects `@voci.trio` when `import trio` fails, both as ordinary `CollectionError`s
([collect.py:127](../voci/_collection/collect.py#L127)) naming the install extra.

**Step 2 — the call branch.** A third branch in the call phase of `_run_envelope`, ahead of the
existing `iscoroutinefunction` check:

```python
elif record.marks.trio:
    returned = await asyncio.get_running_loop().run_in_executor(
        None,
        _safety.track_sync_call(record.id, _trio.call(record.func, call_kwargs, budget)),
    )
```

`_trio.call` returns a plain sync callable that does `trio.run(...)` on the wrapped coroutine
function. `track_sync_call` ([safety.py:254](../voci/_run/safety.py#L254)) is reused unchanged, so
a trio test whose body never returns is named by `stuck_calls` at the end of the run rather than
leaving an unexplained hang. `watch_unawaited` still wraps the whole thing, so an un-awaited
coroutine created inside a trio test still fails it (spec/05 §5).

**Step 3 — timeouts.** The envelope's `asyncio.timeout` can cancel the *await* on the future but
cannot stop the thread, which is the documented sync-test caveat. For trio it can do better:
pass the effective budget (per-test `@voci.timeout` mark, else `--timeout`) into the wrapper and
spend it as `with trio.move_on_after(budget)`. On expiry the wrapper raises `TimeoutError` from
inside the thread, the future completes, and the existing `except TimeoutError` path
([run.py:317](../voci/_run/run.py#L317)) reports `timeout` with no special-casing. Both deadlines
stay armed — the asyncio one is the backstop for a trio test that blocks its own thread and so
never reaches a trio checkpoint.

**Step 4 — traceback and `raises`.**
- Add `trio/` and `outcome/` to the runner-frame suppression set (spec/10 §19); without them every
  trio failure drags in `_run` / `Runner.run` frames.
- Extend the `raises()` cancellation guard ([raises.py:73](../voci/_assertions/raises.py#L73)) to
  reject `trio.Cancelled` on the same reasoning it already rejects `asyncio.CancelledError` — but
  only when `trio` is already in `sys.modules`, so the guard never imports it.
- Trio's strict nurseries raise `ExceptionGroup`; the reporter already handles groups (a teardown
  with multiple errors produces one), so this needs a test, not code.

**Step 5 — external cancellation (stretch).** `--maxfail` and Ctrl-C cancel the awaiting task, which
abandons the thread. To make them reach the test, have the wrapper capture
`trio.lowlevel.current_trio_token()` and the root `CancelScope` at run start, publish them in a
registry keyed by test id, and have the envelope's `CancelledError` handler call
`token.run_sync_soon(scope.cancel)` before it returns. Worth doing, but separable: without it
trio tests are exactly as interruptible as sync tests are today, which is a shipped and documented
limitation rather than a regression.

**Step 6 — docs and matrix.**
- A `docs/how-to` page: the mark, the extra, and the two limitations (fixtures, cancellation).
- ROADMAP "Working today" entry.
- [matrix.py:1013](../voci-migrate/voci_migrate/matrix.py#L1013): `pytest-trio` moves from
  `VC323` ("voci runs tests on asyncio") to `VC320` with the `@voci.trio` recipe. `pytest-tornasync`
  stays `VC323`.

## 4. What this does not give you

**trio-native async fixtures.** Fixtures are constructed by `_di` on the asyncio loop
([runtime.py:315](../voci/_di/runtime.py#L315)) and their values are handed across the thread
boundary into `trio.run`. A sync fixture is fine. An `async def` fixture that only awaits
loop-agnostic things is fine. An async fixture that opens a nursery, or yields a trio resource
bound to the trio run that no longer exists by the time the test starts, is not — and voci cannot
detect the difference, so this is a documented rule, not an enforced one. Suites that push
setup into trio fixtures will feel this; suites whose fixtures are DB/HTTP/tmpdir plumbing will not.

**The loop watchdog does not cover trio tests.** A blocking call inside a trio test stalls its own
thread, not the shared loop, so [safety.py](../voci/_run/safety.py)'s heartbeat correctly stays
quiet. `stuck_calls` is the diagnostic instead.

**Threads spawned by `trio.to_thread.run_sync`** come from trio's own limiter, outside voci's
pool and outside its context propagation. Output from them is unattributed.

## 5. Estimate

3–5 days. Steps 1–4 are ~2 days of implementation against existing seams, plus tests: pass / fail /
`voci.Skipped` / xfail through `trio.run`, timeout expiry, fixture injection, capture and log
attribution from inside the trio thread, `ExceptionGroup` rendering, the missing-trio collection
error, and traceback frame suppression. Step 5 adds ~1 day and its own interrupt tests. No existing
asyncio behaviour changes, so the regression surface is the mark plumbing and one new call branch.

## 6. Option B, and why not

The alternative is porting the runner to anyio so the whole session can run on a trio backend.
The mechanical parts map 1:1 — TaskGroup, timeouts, subprocess ([isolated.py:143](../voci/_run/isolated.py#L143)),
Condition, Event — and `ScopeStore`'s `asyncio.Future` single-flight
([runtime.py:114](../voci/_di/runtime.py#L114)) becomes an Event plus a stored result in half a
day. Three things are not mechanical. First, cancellation: the per-test envelope catches
`CancelledError`, converts it to `timeout` or `interrupted`, *un-cancels* the task via
`stop.claim()`, and then keeps running teardown in that same task
([run.py:319-345](../voci/_run/run.py#L319-L345)) — trio forbids all of that, since `Cancelled`
must reach its scope, so the envelope has to be restructured into nested `CancelScope`s with a
shielded teardown scope. That is the most heavily tested behaviour in the codebase. Second, sync
tests: `loop.set_default_executor` ([run.py:1023](../voci/_run/run.py#L1023)) has no trio
analogue, so `ContextPropagatingExecutor` needs a parallel implementation and a user's
`asyncio.to_thread` stops landing in voci's pool. Third, the abort path — `asyncio.all_tasks`,
`set_exception_handler`, closing the loop under pending tasks
([run.py:1039-1105](../voci/_run/run.py#L1039-L1105)) — is asyncio escape hatches with no anyio
surface, so second-Ctrl-C abort needs a trio-specific design. Add running the whole suite twice in
CI (12 test files reference asyncio directly), rewriting spec/05 and spec/12, and losing uvloop as
the documented performance story, and it is 4–6 weeks to rewrite voci's subtlest subsystem for a
minority of suites. Option A serves those suites in a week without touching any of it.
