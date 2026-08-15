# Rationale

Why velox is shaped the way it is. This is the document to read before changing anything
load-bearing — each decision here has a failure mode behind it, and the code alone won't tell you
what it is.

It covers the important choices only. For what exists, read [../README.md](../README.md); for what
doesn't, [../ROADMAP.md](../ROADMAP.md).

## The shape of the system

```
argv + [tool.velox]  →  discovery  →  collection  →  DI plan  →  execution  →  reporting
```

One pass, one direction, no node tree. Discovery is a directory walk and a name filter. Collection
imports each test file and produces a flat `list[TestRecord]`. Each record gets a static resolution
plan for its entire transitive fixture graph. Execution dispatches those records as concurrent
tasks on one event loop. Reporting turns the results back into logical order.

The pieces cross-cutting that pipeline — capture sinks, log routing, assertion state, FastAPI
dependency overrides — are all routed the same way, through `ContextVar`s. That is the single most
important structural fact about this codebase, and the next section is why.

## Everything per-test lives in a ContextVar

The runner has no module-global mutable state. Anything that varies per test is a `ContextVar`;
anything genuinely shared is owned by an explicit object passed down.

This is not a style preference. A test runner is full of "the current X" — the current capture
sink, the current assertion explanation hook, the current set of dependency overrides. Every one of
those is a module global in a sequential runner, and every one of them is a race the moment two
tests are in flight. `ContextVar` makes them safe by construction rather than by locking: each
`asyncio.Task` gets an independent copy of the context at creation, so a `set()` inside one test
mutates only that test's view, no matter how the `await`s interleave.

The practical consequence for anyone writing code here: **never reach for a module-level mutable**,
and never add an install/uninstall or suspend/resume dance around per-test state. If you find
yourself needing one, the state is in the wrong place.

## One process, one event loop

Tests run as concurrent `asyncio` tasks in a single process. The alternative — worker processes,
as `pytest-xdist` does it — multiplies every shared resource by the worker count: N connection
pools, N copies of every import, N session-scoped fixtures that were meant to be one. For a suite
whose cost is I/O wait, concurrency inside one process gets the same wall-clock win without any of
that, and keeps session scope meaning what it says.

The cost is real and worth stating plainly: one blocking call anywhere in user code stalls every
test at once. That is why a loop-starvation watchdog is a headline roadmap item rather than a nicety,
and why `--concurrency 1` is documented as the first debugging step.

## Fixtures are imported, not resolved by name

pytest finds a fixture by matching a parameter name against everything visible through the
`conftest.py` chain. velox requires you to import the function and name it in a `Depends()`
default, exactly as a FastAPI route does.

What this buys: "go to definition" works, renames are safe, unused fixtures are visibly unused, and
a typo is an `ImportError` at collection instead of a fixture-not-found at run time. What it costs:
you write the import. There is no `conftest.py` at all, and adding one back would undo the whole
trade.

The injection plan is read from `__code__`/`__defaults__` directly — never `inspect.signature`, and
never by unwrapping `__wrapped__`. That keeps collection fast and predictable, at the price of one
known sharp edge: a decorator that replaces a test's signature with `(*args, **kwargs)` hides its
`Depends()` defaults from collection entirely.

The line is drawn at name-based resolution, not at where a dependency is declared. A container —
a test module, or a package `__init__.py` covering that directory and below — can declare fixtures
on behalf of every test inside it, with `velox.use(...)`:

```python
velox.use(db_reset)
```

The fixture is still an imported object, named at a site you can jump to; the declaration lives on
the container rather than in each signature, and its value is discarded. FastAPI draws the same
line with `APIRouter(dependencies=[Depends(...)])`. What a reader gives up is that a test's
dependencies are readable from its signature plus the headers of the files above it rather than
from its signature alone; what the DI system keeps is everything the static half rests on — no
lookup that can fail at run time, no shadowing rule, and a `ResolutionPlan` that still describes
the whole graph, since a declared fixture becomes a step like any other, just one nothing in
`root_args` points at.

Declarations accumulate rather than override. A package's apply ahead of those of the modules
under it, and there is no proximity rule by which one can replace another — that rule is the
machinery that makes a conftest chain hard to read, and without it, finding what reaches a test is
reading each `__init__.py` on the way down, in order. A test that should not have a fixture goes
where nothing declares it.

## Logical order governs all output

Collection order and completion order are entirely separate. Under concurrency the second is
nondeterministic — so nothing user-visible is ever allowed to depend on it. Failure details, the
short summary, and every machine-readable emitter are sorted back into logical order (path, line
number, parameter index) before anything is printed.

Two runs of the same test set produce byte-identical output, modulo timings and paths. Anything
that leaks completion order into output is a bug, not a cosmetic issue: it makes CI diffs useless
and makes concurrency look flaky when it isn't.

## One aggregated result per test

setup, call and teardown are internal phases. Exactly one `TestResult` reaches the reporter, and it
is JSON-round-trippable from the first commit. pytest emits three reports per test and every
consumer downstream re-aggregates them.

Serializable results are not speculative future-proofing — they are what makes a per-test
subprocess tier, `--report-json`, and any eventual multi-process mode possible without touching the
reporter, and they make the runner testable without a terminal.

## No plugin system

There are no hooks and no entry-point scanning. Extension happens through dependency injection: a
fixture is the unit of composition, and it is an ordinary function you can import, wrap, or replace.

Two reasons. Entry-point scanning at startup is most of a test runner's cold-start budget, and cold
start is a first-class constraint here. And a hook system is a permanent compatibility surface —
every hook is a promise about internal structure, which is exactly what a runner this young cannot
afford to make.

## Assertion introspection is vendored, not reimplemented

velox vendors pytest's assertion rewriter rather than writing one. Assertion introspection is
mature, subtle, and the single feature users would most notice missing; there is nothing to gain
from a second implementation.

The vendored tree under `velox/_assertions/_vendor/` is **generated** and kept byte-identical to
upstream apart from a recorded edit list — the substantive one being that three module globals (the
comparison hook, the pass hook, and the verbosity config) became `ContextVar`s, for the reason in
the ContextVar section above. Upstream saves and restores them around each synchronously-run test;
under concurrency they would leak one test's assertion config into a sibling's failure message.

The seam is a lookup substitution, not a patch: the vendored code still refers to those names
exactly as upstream wrote them, and a module-level `__getattr__` resolves them to the ContextVars.
That is what lets the tree stay byte-identical. It is regenerated with `just vendor`, never edited
by hand, and excluded from lint and type checking — edit it directly and the next regeneration
silently reverts you.

## Escalate, never silently degrade

Where a mechanism can't be made concurrency-safe — raw global patching, file-descriptor capture,
per-test warning filters — velox escalates the affected test to a stricter tier or fails loudly and
names the cause. It never quietly does the unsafe thing.

The companion rule: a silent pass is a bug. A selector that matched nothing, a test collected as
zero tests, an un-awaited coroutine — each of those should be a distinct failure or exit code, not
a green run. Several of these are still unenforced (see [../ROADMAP.md](../ROADMAP.md)); each one
that stays unenforced is a real defect, not an accepted limitation.

# Per-module notes

Decisions that look like clutter until you know what they prevent. Each one has been reverted by
somebody tidying up, at least in spirit.

## `_collection/collect.py` — import and collection

**Each test file is imported under a synthetic module name.** The name is derived from the path
relative to rootdir, and `sys.path` is never touched. This is what lets two `test_utils.py` files in
different directories coexist in one run — they get different dotted names instead of colliding in
`sys.modules`. The escaping matters for the same reason: `api-v2` and `api_v2` must not collapse to
the same identifier, so an escaped segment carries a short content digest. Simplify that to a plain
`str.replace` and the collisions come back.

**The rewrite hook is consulted by hand during import.** `importlib.util.spec_from_file_location`
never looks at `sys.meta_path`, so building a spec that way silently skips assertion rewriting no
matter how it was installed. `_import_module` therefore asks the installed hook's `find_spec`
directly before falling back. Remove that call and rewriting stops working for every test while the
reported mode still says `rewrite` — no exception, no failing test, just worse assertion messages.

**A class-grouped test gets its receiver built per call, through a hand-written wrapper.** A
`class Test*` is namespacing and nothing else, so the instance a method runs on is constructed for
that one test and discarded — anything shared through `self` would be shared between concurrently
running tests, which is the one thing the grouping must not buy. The wrapper that does this copies
`__name__`, `__qualname__`, `__wrapped__` and the marks by hand rather than using
`functools.wraps`, which copies `__dict__` wholesale: `mock.patch` keeps its `patchings` list
there, and a copy of it on the wrapper is counted a second time by `patching_of`, doubling both
the reported patch targets and the positional arguments they are taken to supply — which then
hides a real missing injection behind a parameter velox believes a mock will fill.

**A test shape that would collect as nothing is a collection error.** A `Test*` class velox can't
construct, a `setup_method` that would never run, a mark on a class, a `test_*` name bound to a
lambda, a test that yields — each of these is silent in the worst way: the suite looks green
because tests are missing from it, or because a test ran without the setup it was written to
expect. Every rule for reporting them is deliberately narrow, because the cost of a false report
is a collection error on working code: a class is reported for being misnamed only when it reads
as a suite (`unittest.TestCase`, or a name ending in `Test`/`Tests`/`TestCase`) *and* no group
inherits it, and a `test_*` name is reported only when it is bound to a function — `test_app =
FastAPI()` and `test_client = Mock()` are callable, ordinary, and nobody's test body.

**A group's shape is read across its whole MRO.** Test methods, `__init__` and lifecycle hooks
are all resolved the way an attribute lookup would resolve them, not off the class body alone.
Reading only `vars(cls)` silently drops every test a shared base contributes — the standard
"one suite, run against three backends" layout — and lets an inherited `setup_method` through the
guard whose entire job is catching setup that will never run.

**`@velox.parametrize` shares one resolution plan across every expanded case.** `plan_for` runs
once per test function, not once per case: a parametrized value is a call kwarg, not a DI graph
node, so building the plan per case would repeat identical work for nothing. `parametrize.
known_params_of` computes what names parametrize supplies before that one `plan_for` call, so
`_check_missing_injections` doesn't mistake a case's own arguments for uninjected fixtures — and
the same pass rejects a name two stacked `@parametrize`s both claim, or one that collides with an
actual `Depends(...)` injection, since either would otherwise fail confusingly later, at call time,
once two sources tried to supply the same keyword.

## `_di/fixtures.py` / `_di/runtime.py` — dependency injection

**The refcount is reserved before the await, not after.** A waiter parked on a pending fixture
future contributes nothing to the refcount until it wakes. If the increment happened after the
await, the constructing caller could acquire, release, and tear the entry down to zero while the
waiter was still suspended — handing it a value whose closer had already run. A failed reservation
is refunded in the matching `except` rather than never taken, because a waiter cannot discover a
pending future already failed without awaiting it.

**A failed entry stays in the store.** Later requesters of the same key must replay the same cached
exception instead of re-running a `build()` that will fail again. Both `release` and `aclose` leave
failed entries alone. Cleaning them up eagerly looks tidier and is wrong.

**Teardown order relies on a precondition that lives outside the class.** `aclose` tears down in
reverse insertion order, which is only valid because `setup` walks an already-topologically-sorted
plan forwards and `build()` never calls back into `acquire`. Every dependency is therefore inserted
before its dependent. Any future feature where a fixture resolves a dependency lazily *during* its
own construction breaks this without changing a line of `aclose` — whoever adds one has to revisit
this method.

**`"call"` scope ranks equal to `"function"`, not below it.** The compatibility check cares about
lifetime, and both tear down at end of test. Ranking `"call"` lower would reject valid graphs over a
caching distinction the check was never meant to police.

**A parametrized fixture's case value has no `request` object to travel through.** velox never
grows one (`_check_missing_injections` has no name-based fallback to hang it off), so `params=`
reuses the convention `@velox.parametrize` already established: the value arrives as an ordinary
argument, name-matched at collection time rather than injected. Fixing that name to `param`
instead of letting it be configured per fixture keeps a parametrized fixture's body readable
without a decorator argument to cross-reference, and keeps `expand_cases` from needing to carry a
name alongside every case value.

**A specialized plan's cache key covers a step's actual parametrized ancestors, not every case in
play.** `expand_cases` could have folded the whole chosen combination into every downstream step's
key uniformly; instead each `PlanStep.param_ancestors`, computed once during `plan_for`'s own
graph walk, tracks exactly which parametrized fixtures that step's own construction transitively
reaches. Two fixtures parametrized independently of each other therefore still share a downstream
step's cache entry across whichever axis it doesn't depend on, rather than needlessly rebuilding it
once per combination of both.

## `fastapi.py` — per-test dependency overrides

**The app stays a singleton; the *view* of it becomes per-test.** `dependency_overrides` and
`state` are plain per-app-instance data, so concurrent tests against one module-level `app` all
write the same dict — and the documented teardown idiom, `clear()`, wipes everyone else's overrides
too. The alternative usually recommended is an app factory, which is a change to production code
made solely for tests.

velox swaps each attribute once, per app, for a proxy layering a `ContextVar` of per-test values
over what was already there. Three upstream facts make it work, all verified by reading the source
rather than assumed: a route stores only a *pointer* to the app and resolves the override at
request-solve time via `.get(call, call)`; `request.app` comes from the ASGI scope, which Starlette
repopulates from the singleton on every request, so installing the proxy after routes exist still
catches everything; and `ASGITransport` awaits the app in the calling task, so a request inherits
the test's context. Swapping in a per-test app instance instead would break the first fact — routes
point at *this* app, so a copy is a different app, not an isolated view of the same one.

## `_builtins/fixtures.py` — built-in fixtures

**`set_level`'s concurrency hazard is asymmetric.** Logger levels are process-global. Raising a
level can make a sibling capture more than it expected — benign for "this record is present",
harmful only for "nothing was logged". *Lowering* one, to silence a noisy dependency, can make a
sibling's own `set_level(DEBUG)` block capture nothing at all for a record it definitely emitted.
Whether this API is "basically safe" depends on which direction the level moves.

**The `_builtins/fixtures.py`/`_builtins/capture.py` import cycle is deliberate.** The built-in
fixture functions here are stubs; their real providers live in `capture.py`, which imports the
four result types back from this module. The cycle resolves only because the rewiring import sits
at the *bottom* of `fixtures.py`, after every name `capture.py` needs already exists. Move it up
with the other imports and you get a partially-initialized-module `ImportError`.

## `_run/run.py` — execution

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
leak. A plain `with asyncio.Runner()` reintroduces that noise on exactly the Ctrl-C path velox most
needs to exit cleanly.

**`AdmissionGate` decides concurrency, `exclusive=`, and `solo` together, not as three layered
gates.** Bounding concurrency with a plain semaphore and admitting exclusivity/solo through a
second gate behind it would put the wrong thing in `_running`: a test piled up waiting on a
contended token or a solo lock would already hold a concurrency slot while it waits, so enough
same-token tests could fill every slot with waiters and starve every *unrelated* test out of the
suite too — the opposite failure from the one solo admission has to avoid. Deciding all three in
one `wait_for` predicate means a waiting test holds nothing: it isn't counted in `_running` and
doesn't occupy a slot, so unrelated tests are admitted around it exactly as if it didn't exist. A
test's exclusive-token set is acquired and released as one atomic step, for its whole footprint at
once, never one token at a time, so two tests can never deadlock each holding a token the other is
waiting for.

**`AdmissionGate.release` is shielded from cancellation.** Every other per-test cleanup step in
`dispatch_one` (worker-slot release, `current_test_context.reset`, module-scope teardown) is either
synchronous or already swallows a collateral `CancelledError` into an `ERROR` result rather than
letting it escape — `run_suite`'s own invariant is that nothing but `KeyboardInterrupt`/
`SystemExit` ever leaves a dispatched task. `release`'s state update requires acquiring the gate's
lock first, and that `await` is exactly where a collateral cancellation (a sibling's interrupt
propagating through `asyncio.TaskGroup`, or `on_result` raising) can land, before the
decrement/notify ever runs. Skipping that update, unlike skipping some other cleanup, doesn't just
affect the one test: it leaves `_running`/`_running_tokens` permanently wrong, which can deadlock
every other test still waiting on the same gate for the rest of the run. `asyncio.shield` lets the
cancellation reach the caller immediately while the update finishes in the background, so the
caller's own cancellation semantics are unchanged but the gate's bookkeeping can't be left
half-done.

**A test's `--timeout`/`@velox.timeout(...)` budget starts inside `_run_one`, after admission, not
at dispatch.** Time spent waiting for `AdmissionGate.acquire` — behind a `@velox.solo` test, or
behind another test holding the same `exclusive=` token — is not counted against it. Starting the
clock at dispatch would turn "this test's setup/call/teardown took too long" and "this test waited
behind a contended resource" into the same `TIMEOUT` outcome, though they point at unrelated fixes:
raise the budget or find the blocking call, versus reduce contention or accept the wait.

**Stopping early cancels; it does not wait.** `--maxfail` and a Ctrl-C both mean the run is over,
and a run that keeps waiting for the tests already in flight is only as fast to stop as its
slowest one — under concurrency that is routinely the whole point of the flag, spent waiting.
`StopController` cancels every admitted test instead, and a cancelled test reports `CANCELLED`
rather than `FAILED`: velox stopped it, so it never got to say anything about the code under
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
swallows its cancellation cannot be taken off the loop, so velox names the tests still holding the
run open and leaves the second Ctrl-C as the answer.

**velox owns `SIGINT` for the duration of a run.** Left to Python's default handler a Ctrl-C
raises `KeyboardInterrupt` wherever the main thread happens to be, which aborts the run mid-flight
and throws away the report for everything that did finish — the most useful thing an interrupted
run has. The handler installed here turns the first Ctrl-C into the same stop `--maxfail`
performs, from the loop, so fixtures unwind and the reporter still prints. The second is the
escape hatch for a test that ignores cancellation and for a loop too blocked to process the first,
and it takes the abrupt path deliberately: `asyncio.Runner.close()` cancels what is left and then
*waits* for it, which is exactly what a test that already ignored one cancellation will not
honour, so the loop is closed out from under it instead. The handler is restored on the way out,
and never installed off the main thread, where `signal.signal` is not allowed.

## `_run/safety.py` — the loop watchdog and tests that check nothing

**The watchdog reports from a thread, because the loop is the thing that cannot report.** A
blocking call inside an `async def` test holds the one loop every concurrently dispatched test
shares, and from the loop's own perspective nothing is happening at all — no callback can run to
notice, which is why an unwatched stall reads as velox hanging rather than as a test misbehaving.
A daemon thread reading a heartbeat the loop leaves behind is the only vantage point that still
works while the loop is held, and `sys._current_frames()` is what turns "something is stuck" into
a file, a line, and a test id. It warns and never fails a test: velox cannot tell a blocking call
apart from a fixture that legitimately takes a while, and a diagnostic that can be wrong must not
be able to fail a build.

**A reported stack is cut at velox's own innermost frame.** Everything outside it is velox
dispatching a test and asyncio dispatching velox — the same dozen frames on every report, none of
which answer the question. Everything inside is the test's own call chain, which does.

**Un-awaited coroutines ride CPython's own warning rather than a coroutine tracker.** The
interpreter already reports every coroutine whose last reference goes away un-started, at the
moment it goes away — which for one a test created is inside that test's own call phase. Routing
`warnings.showwarning` to whichever test is running is a `ContextVar` read; tracking coroutine
creation instead would mean a `sys.setprofile`-class hook on the hot path of every test in the
suite to catch a mistake that, once caught, is a one-line fix. The trade is that a coroutine
deliberately kept alive past the end of the test that made it is filed against whoever is running
when it is finally collected. The filter is set to `always` rather than Python's default of once
per source location, since a parametrized test forgets its `await` at the same line in every case.

**A sync test stuck in a worker thread is named, not waited for.** Nothing in Python can interrupt
a thread: cancelling the future that awaits one abandons the wait, not the call. So the executor
never joins on shutdown — waiting there would make the end of a run, and a Ctrl-C especially, take
exactly as long as the blocking call that made the run worth abandoning — and velox instead
prints which test is still running and where it is blocked. The interpreter still cannot exit
until that call returns; the difference is whether the user is told why.

**A test that returns a value or drops a coroutine fails regardless of `@velox.xfail`.** `xfail`
re-reads what the call phase *raised*. Neither of these raises anything: they are a test that ran
to the end while checking nothing, which no mark can have predicted and no `raises=` can match.

## `_mocking.py` — `unittest.mock` patching

**velox detects patching and schedules around it rather than shipping a patch API of its own.**
Mock *objects* — `MagicMock`, `AsyncMock`, `create_autospec`, the call assertions — are
per-instance state and already concurrency-correct, so there is nothing to replace. The installer
is the whole problem: `mock.patch` does a real `setattr` on a module or class, and every test
running at that moment sees it. A velox-branded patch decorator that also had to run solo would be
`unittest.mock` with a different import line and a migration cost, so velox delegates the patching
and owns only the scheduling. The cost lands in the summary — number of tests drained and the wall
clock they held — because a suite that drifts into a hundred solo tests has lost the concurrency it
adopted velox for, and should read that off the report rather than a stopwatch.

**A test's injection plan is read through its decorators, and the decorated object is what runs.**
`plan_of` reads `__code__`/`__defaults__`, and a decorator's wrapper is `(*args, **kwargs)` with no
defaults at all, so a `@mock.patch`-decorated test presents zero parameters: every `Depends(...)`
default on the real function underneath goes unseen, and the test is called with none of them —
Python's own fallback then hands the parameter the unresolved `Depends(...)` sentinel. Silent, and
exactly the failure shape velox refuses elsewhere. `real_function` unwraps to the function actually
written (also where its definition line lives, which is what keeps a decorated test in definition
order) while `TestRecord.func` stays the wrapper, since applying the decorator is the point.
`unittest.mock` fills its mock arguments in ahead of velox's keyword arguments, positionally, so
`plan_for` is told how many leading parameters are already spoken for; a `Depends(...)` declared in
one of those slots is a collection error rather than a mock silently arriving where a fixture
belongs. `mock.patch.multiple` is the exception that fills its parameters by name, which is what
`@velox.parametrize` already does, so those names join the same set of externally supplied
arguments.

**`with mock.patch(...)` inside a body fails the test instead of racing it.** There is no patcher
object to find at collection — it does not exist until the line runs — so the only place left to
catch it is where it installs, and the only alternative is a silent race whose symptom shows up in
whichever *other* test happened to read the patched attribute. The guard wraps `unittest.mock`'s
own `__enter__` for the duration of a run and raises before anything is written, so the failure
names the target and the line, and the patch never reaches the module. Every out is a mark the
message names: `@velox.solo` for the test that patches, `@velox.isolated` for a target with no
per-task view at all. A patch entered with no test running — an import, a session fixture — is
nobody's hazard and passes through.

**The guard is best effort by construction.** It replaces a method on a private `unittest.mock`
class, which is not API: `install` degrades to leaving patching undetected if those internals move,
rather than failing a run over a stdlib change, and it is installed only when the suite has
imported `unittest.mock` at all, so a suite that never mocks pays nothing for it. The same
best-effort reasoning covers `mock.patch.dict`, whose decorator records no `patchings` list and can
only be recognized by the patcher its wrapper closes over.

## `_run/isolated.py` — the `@velox.isolated` subprocess tier

**The subprocess re-collects from source; it is never handed the parent's live objects.** A
`TestRecord`'s `func` and `plan` are ordinary Python objects — closures, DI providers, imported
modules — with no general JSON (or pickle-safe) representation, and forking the parent to clone
them would inherit exactly the process-global state (signal handlers, loop policy, an open thread
pool mid-run) `@velox.isolated` exists to escape. The subprocess is instead handed just a file path
and a target test id, imports that one file fresh, and calls `_collect.collect` on it again —
paying a real re-import cost per isolated test, deliberately, in exchange for a genuinely clean
interpreter rather than a copy of a busy one.

**The wire format is a dict, not a `TestResult`.** `isolated.py` cannot import `run.py`'s
`TestResult`/`Outcome` at module level — `run.py` imports `isolated.py` to dispatch a test there in
the first place, so the reverse import would be circular. `result_to_json`/`_result_from_json`
split the (de)serialization across the boundary instead: the worker (which already imports `run.py`
to actually run the test) builds the dict, and `run.py`'s own `dispatch_one` rebuilds the
`TestResult` from it, with `isolated.py` itself staying a leaf module.

**A child's own `tmp_path` root is nested under the parent's, never passed to it directly.**
`_capture.install`'s explicit-`basetemp` path unconditionally clears whatever directory it's given
before use — correct for a top-level `--basetemp`, catastrophic for an isolated test's subprocess,
which would otherwise wipe the parent's basetemp root out from under every other test still running
concurrently. Each isolated test gets `basetemp_root/isolated/<sanitized-id>` instead: a path
`run_isolated` computes but deliberately never creates itself, so the child's own `install()` call
is the first thing to touch it, finds nothing there, and does a plain `mkdir` with no `rmtree`.

**A crashed or cancelled subprocess still fills `results[index]`.** `run_suite`'s documented
contract is that every dispatched test ends up with a result, whatever went wrong. `run_isolated`
upholds it the same way `_run_one` does for an in-process test: a subprocess that exits non-zero, or
never writes its result file, or is killed by a collateral cancellation from a sibling's
`KeyboardInterrupt`/`SystemExit`, is folded into an `error` result rather than left to leave that
slot empty or the whole run hanging.

## `_builtins/capture.py` — capture and routing

**Output from an orphaned background task can vanish.** A task created with `create_task` and never
awaited inherits the test's context, so it keeps writing into that test's sink after the test has
finished and the runner has moved on. If the test passed, that sink is never read again and the
output reaches neither the test's captured output nor the unattributed section. This is not fixable
here — closing it means making a test's background tasks part of its envelope, which is a
scheduling change. When someone reports a missing log line, look for an orphaned task before
suspecting capture.

**The retention sweep asks who is alive, not who is newest.** Every velox on a machine allocates
its session root under one directory per user, and sweeps that directory as it starts. Numbering
alone can't say which roots are free to delete: the keep window counts runs, so three runs started
elsewhere are enough to push a live root out of it, and deleting it takes that run's `tmp_path`
directories — and an `@velox.isolated` test's config file — with it. Every root therefore carries a
lock naming the process holding it, and a root whose owner still answers is spared however old it
is. The pid is the primary signal because it settles both directions immediately; `LOCK_STALE_AFTER`
is only for the cases where the pid can't be probed at all — the machine rebooted, or the platform
turns the probe into a kill.

**`_CappedBuffer` is the one thing in this module that needs a lock.** Everything else is race-free
because only one task at a time holds a given sink's context. That argument does not hold for the
buffer itself: `ContextPropagatingExecutor` exists precisely so a test's `run_in_executor` call can
write into the *same* sink from a worker thread while the test's own task writes from the loop
thread. Removing the lock to match the module's otherwise lock-free style reintroduces a real
read-modify-write race.

**The converse, for the structures that genuinely need no lock.** `WorkerSlots`' free list and
`TmpPathFactory`'s per-basename counter are both shared across concurrent tests and both
unsynchronized, because neither `await`s between reading and writing: asyncio is single-threaded,
so no rival task can interleave a step in between. The guarantee is about the absence of a
suspension point, not about the operation being small — make any part of either path `async`, or
move it off the loop thread, and it needs revisiting.

## `_assertions/rewrite.py` — assertion introspection

**A fallback to `plain` is recorded as data, not just warned about.** When the pyc cache probe
fails, `plan` prints to stderr *and* records the reason on `AssertionSetup`, which the report
header then surfaces. A stderr warning is easy to miss in CI, and a benchmark run that silently
degraded to `plain` measures the wrong thing with nothing in the numbers to say so.

## `cli.py` — entrypoint

**Flags default to `None`, not to their real defaults.** Layering CLI over `[tool.velox]` over the
built-in default requires distinguishing "the user typed `--concurrency`" from "argparse filled one
in". A concrete argparse default erases that distinction and makes the config file's value
unreachable whenever the two happen to match.

**`main()` leaves no global state behind, on any exit path.** It installs the rewrite import hook,
prepends `rootdir` to `sys.path`, and applies `[tool.velox] env` to `os.environ` — and undoes all
three in one `finally`, each restoring only what this call changed rather than resetting to a fixed
state. `main()` is called repeatedly in-process (this package's own suite does it), so a missed
restore leaks a stale `sys.path` entry shadowing a same-named package, or one suite's environment
into the next.

**`--basetemp` is validated in two places on purpose.** `cli.py` does a cheap path-shape check (cwd,
ancestors, home, root) so a typo fails fast and clean; `_capture.install` checks for its marker
file. Both guard the same catastrophe — an unguarded `rmtree` on a wrong path — and neither
subsumes the other, since only CLI callers reach the first and only the second protects callers
that use `_capture`/`_run` directly.

## `_report/terminal.py` — reporting

**The reporter counts records, it does not build a dict.** A factory-generated test can repeat its
id, so two records may legitimately share one id within a file. Deriving per-file counts from a
dict keyed by id collapses those, undercounts the file, and flushes its scrollback block early —
before every test has reported in.

**`stream` is bound once, at construction.** By the time results arrive, `sys.stdout` has been
replaced by the capture `Router` and the current test's context has been reset. A `print` that
resolved `sys.stdout` lazily would land in the session sink, and every per-file block would
disappear from live output and resurface at the end under unattributed output — with nothing about
the symptom pointing back here.

**A default run's length is bounded by how many files it collected, not how many tests it
skipped.** A skip contributes a count to its file's block; the reasons are a `-v` section. Those
reasons are worth reading once, when a skip is written or when someone goes looking for it, and
never on the hundred runs in between — a suite with fifty long-standing skips otherwise buries its
file blocks under fifty lines that say nothing new about this run.

**The counts land in one place, once.** Only the reporter prints them, and only at the end: the
per-outcome breakdown, the totals, and the wall-vs-concurrency ratio are all one derivation of one
result list. Two summaries computed from the same run in two modules will eventually disagree about
what "failed" means, and a reader with two lines of counts in front of them has to work out which
one is the answer.

**Zero counts are left out.** A category prints only when it has something to report, so a clean
run's totals carry the four or five numbers that describe it rather than every bucket velox knows
about. What survives is the line's real job: the totals a reader checks at a glance, and — on a run
with failures — a line above it holding nothing but what went wrong.

**The path column is measured once, from every path the run collected.** Every file is known before
the first block prints, so the column can be sized to the deepest path in the suite instead of to a
constant that a real tree outgrows on its first `tests/api/v2/` directory. It is measured up front
rather than per block because widening it mid-run would leave every block already on screen ragged
against the ones below it, and clamped at both ends so a shallow suite still gets a column and a
pathological path is elided rather than pushing the durations off the terminal.

