# `_run/run.py` — execution

See [rationale.md](../rationale.md) for the index.

**A timeout is its own outcome, and it takes two checks to detect.** `TIMEOUT` is never folded into
`FAILED` or `ERROR` because the remedy differs: raise the budget or find the blocking call, rather
than fix the test. Detecting it is not just catching the `TimeoutError` from
`asyncio.timeout.__aexit__` — a test can intercept the injected `CancelledError` and substitute its
own exception before that ever happens. `_run_one` therefore also consults `deadline.expired()`,
which CPython sets whenever the deadline fired, including in the branch where it declines to raise.
Drop the cross-check and every catch-and-substitute test silently reports `FAILED` instead. Neither
mechanism can preempt a test body that never reaches an `await` — cooperative scheduling has
nothing to interrupt.

**A sync test runs on the executor; a sync fixture runs inline.** These look like the same
decision made twice, backwards. They aren't: a fixture's sync body is typically a quick setup
step feeding straight into the test that depends on it, so paying a thread hop buys little. A
test's own body is the one place a suite author writes the slow, blocking call on purpose —
`time.sleep`, a sync DB driver, `requests`. Calling it inline would stall every other
concurrently-dispatched test sharing the one event loop for as long as it runs; dispatching it to
`_capture.ContextPropagatingExecutor` via `run_in_executor(None, ...)` costs that test's own
concurrency slot instead of everyone else's. Neither path can be preempted by the test's own
`--timeout` once the call starts — cooperative cancellation has nothing to interrupt an `await` a
sync body never reaches, and a thread pool worker can't be killed out from under it — so the
budget still elapses and the result still reports `TIMEOUT`, just with the thread finishing out of
band afterward.

**`--maxfail`'s counter is bumped inside the gate, before the release.** Releasing the admission
gate is what admits the next queued test, so a failure counted after the release races that test's
own check of the flag and lets it start anyway. Every dispatched task also checks the flag once
before queueing, which is worth nothing on its own: the tasks are all created together and reach
that first check before any result exists. The check that decides is the one after admission.

**Module-scope fixtures are released by the suite, not by the test.** Releasing a module's fixtures
when a test's own teardown runs would tear them down as soon as the *first* of that module's
concurrent tests finished, while its siblings still held live references — `scope="module"`
quietly degrading to `scope="function"`. `run_suite` counts each module's tests up front and
releases only at zero, which also guarantees no release ever overlaps an acquire of the same key.
This is why `_di.setup`'s partial-failure cleanup is told not to release module-scope keys:
`run_suite` is the only thing that ever releases one.

**`asyncio.Runner` is closed by hand, under `suppress(RuntimeError)`.** With a custom default
executor installed and a `KeyboardInterrupt` propagating out of the last `run_until_complete`,
`Runner.close()`'s automatic executor shutdown raises a spurious `RuntimeError: Event loop stopped
before Future completed` — loop bookkeeping tripping over an exception-driven exit, not a real
leak. A plain `with asyncio.Runner()` reintroduces that noise on exactly the Ctrl-C path voci most
needs to exit cleanly.

**`AdmissionGate` decides concurrency, `exclusive=`, and `solo` together, not as three layered
gates.** Bounding concurrency with a plain semaphore and admitting exclusivity/solo through a
second gate behind it would put the wrong thing in `_running`: a test piled up waiting on a
contended token or a solo lock would already hold a concurrency slot while it waits, so enough
same-token tests could fill every slot with waiters and starve every *unrelated* test out of the
suite too — the opposite failure from the one solo admission has to avoid. Deciding all three in
one `_admits` predicate means a waiting test holds nothing: it isn't counted in `_running` and
doesn't occupy a slot, so unrelated tests are admitted around it exactly as if it didn't exist. A
test's exclusive-token set is acquired and released as one atomic step, for its whole footprint at
once, never one token at a time, so two tests can never deadlock each holding a token the other is
waiting for.

**`release` hands its freed slot to a waiter; it doesn't wake the queue to race for it.** The gate
was an `asyncio.Condition` whose every release called `notify_all`, so every waiter re-tested its
own admission predicate on every release. `run_suite` creates every test's task up front, so the
queue is the whole suite and that is quadratic in its size — measured at ~23s of wall clock for
16k trivial tests, against 0.6s once the release admits directly. `_wake` instead scans from the
head of a `deque` only as far as the free capacity allows and books each admission itself, which
for the ordinary case (a waiter with no exclusive tokens at the head) is one step. It walks *past*
a waiter held up by a token or a solo lock rather than stopping there, since a later waiter may
still fit in the slot that one cannot use — the same barge-ahead the `Condition` had, and the same
absent fairness guarantee (`ROADMAP.md`). Booking the admission inside `release` rather than when
the waiter's coroutine resumes is what keeps two tests from ever being admitted into one slot; the
cost is that a waiter cancelled in the tick between the two has to give the slot back itself,
which `acquire`'s own `except asyncio.CancelledError` does. The mirror of that window is a waiter
cancelled *before* being admitted: `Task.cancel` cancels the future it is suspended on right
there, but the waiter stays in the queue until its coroutine resumes, so `_wake` has to drop an
already-completed future rather than book it a slot — which the old `notify_all` got for free from
`Condition`'s own `if not fut.done()`.

**`AdmissionGate.release` is synchronous.** Every other per-test cleanup step in `dispatch_one`
(worker-slot release, `current_test_context.reset`, module-scope teardown) is either synchronous or
already swallows a collateral `CancelledError` into an `ERROR` result rather than letting it escape
— `run_suite`'s own invariant is that nothing but `KeyboardInterrupt`/`SystemExit` ever leaves a
dispatched task. Any `await` inside `release` is somewhere a collateral cancellation (a sibling's
interrupt propagating through `asyncio.TaskGroup`, or `on_result` raising) can land before the
state update runs. Skipping that update, unlike skipping some other cleanup, doesn't just affect
the one test: it leaves `_running`/`_running_tokens` permanently wrong, which can deadlock every
other test still waiting on the same gate for the rest of the run. Having nothing to await removes
the window outright — where the previous `Condition`-based gate needed an `asyncio.shield` around
its release to reach the same guarantee.

**A test's `--timeout`/`@voci.timeout(...)` budget starts inside `_run_one`, after admission, not
at dispatch.** Time spent waiting for `AdmissionGate.acquire` — behind a `@voci.solo` test, or
behind another test holding the same `exclusive=` token — is not counted against it. Starting the
clock at dispatch would turn "this test's setup/call/teardown took too long" and "this test waited
behind a contended resource" into the same `TIMEOUT` outcome, though they point at unrelated fixes:
raise the budget or find the blocking call, versus reduce contention or accept the wait.

**A timed-out test's teardown is time-boxed too, not just a cancelled one's.** An ordinary test's
teardown has no budget: voci can't tell a fixture that legitimately takes a while from one that
has stopped making progress, and guessing wrong would fail working suites. A test whose
`--timeout` just fired is the case where that guess is already made — the fixture the deadline
landed on is the first suspect for hanging on the way out too, and its teardown runs while the
test still holds its admission slot. Left unbounded there, one hung `finally` hangs the entire run
with nothing reported at all, which is precisely the failure `--timeout` exists to bound. The
cancellation path's `teardown_grace` covers it, and an overrun is reported on the `TIMEOUT` result
and on stderr rather than being waited out.

**The worker pool is sized to at least `concurrency`, and never below Python's own default.**
The floor at `concurrency` is what stops one sync test from waiting for a thread while its own
`--timeout` budget runs down — the gate admits at most that many tests, so that many threads is
enough for all of them. The floor at `min(32, cpu + 4)` is what stops the *other* direction from
biting: this is the loop's default executor, so a test's own `asyncio.to_thread(...)` calls land
in the same pool, and a pool sized to `--serial`'s single slot would deadlock any test that fans
out over two threads and waits for both.

**Stopping early cancels; it does not wait.** `--maxfail` and a Ctrl-C both mean the run is over,
and a run that keeps waiting for the tests already in flight is only as fast to stop as its
slowest one — under concurrency that is routinely the whole point of the flag, spent waiting.
`StopController` cancels every admitted test instead, and a cancelled test reports `CANCELLED`
rather than `FAILED`: voci stopped it, so it never got to say anything about the code under
test, which is a different statement from "it works" and from "it doesn't". The exit code comes
from what stopped the run — `--maxfail` implies the failures that reached the threshold, a Ctrl-C
exits 2 — not from tallying cancellations. A test that has not started when the stop lands is
dropped with no result at all, which is what the "not run" count is derived from.

**A cancelled test's teardown is time-boxed; an ordinary one's is not.** The fixture holding a
container or a connection pool deserves a real chance to release it even when the run is ending,
so cancellation is not the end of the test's envelope. But the thing a cancelled test was waiting
on is often the same thing its fixture will wait on, and an unbounded release would hand the run
right back to whatever made it worth stopping. `DEFAULT_TEARDOWN_GRACE` splits the difference and
says so on stderr when it runs out. Nothing bounds the call phase itself the same way: a test that
swallows its cancellation cannot be taken off the loop, so voci names the tests still holding the
run open and leaves the second Ctrl-C as the answer.

**voci owns `SIGINT` for the duration of a run.** Left to Python's default handler a Ctrl-C
raises `KeyboardInterrupt` wherever the main thread happens to be, which aborts the run mid-flight
and throws away the report for everything that did finish — the most useful thing an interrupted
run has. The handler installed here turns the first Ctrl-C into the same stop `--maxfail`
performs, from the loop, so fixtures unwind and the reporter still prints. The second is the
escape hatch for a test that ignores cancellation and for a loop too blocked to process the first,
and it takes the abrupt path deliberately: `asyncio.Runner.close()` cancels what is left and then
*waits* for it, which is exactly what a test that already ignored one cancellation will not
honour, so the loop is closed out from under it instead. The handler is restored on the way out,
and never installed off the main thread, where `signal.signal` is not allowed.
