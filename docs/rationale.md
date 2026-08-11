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

There is also no `autouse`. Adding a name-based fallback "for convenience" would undo the thing
that makes the static half of the DI system possible: a test's dependencies would stop being
readable from its own signature, and the resolution plan built from `__defaults__` would no longer
describe the whole graph, because something could arrive through the back door. Treat `autouse` as
a different feature with a different cost, not a small addition to this one.

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

The vendored tree under `velox/_vendor/assertion/` is **generated** and kept byte-identical to
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

## `_builtins/capture.py` — capture and routing

**Output from an orphaned background task can vanish.** A task created with `create_task` and never
awaited inherits the test's context, so it keeps writing into that test's sink after the test has
finished and the runner has moved on. If the test passed, that sink is never read again and the
output reaches neither the test's captured output nor the unattributed section. This is not fixable
here — closing it means making a test's background tasks part of its envelope, which is a
scheduling change. When someone reports a missing log line, look for an orphaned task before
suspecting capture.

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

## `_rewrite.py` — assertion introspection

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

