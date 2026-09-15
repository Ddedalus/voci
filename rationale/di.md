# `_di/fixtures.py` / `_di/runtime.py` — dependency injection

See [rationale.md](../rationale.md) for the index.

**The refcount is reserved before the await, not after.** A waiter parked on a pending fixture
future contributes nothing to the refcount until it wakes. If the increment happened after the
await, the constructing caller could acquire, release, and tear the entry down to zero while the
waiter was still suspended — handing it a value whose closer had already run. A failed reservation
is refunded in the matching `except` rather than never taken, because a waiter cannot discover a
pending future already failed without awaiting it.

**A failed entry stays in the store.** Later requesters of the same key must replay the same cached
exception instead of re-running a `build()` that will fail again. Both `release` and `aclose` leave
failed entries alone. Cleaning them up eagerly looks tidier and is wrong.

**A *cancelled* entry does not.** A `build()` interrupted partway — a `@voci.timeout(...)` budget
expiring on whichever test happened to be the one constructing, `--maxfail`, a Ctrl-C — is the one
failure that says nothing about the fixture. Caching it makes one test's deadline every later
test's `CancelledError`: a shared `session`-scope fixture is only ever constructed once, so the
whole suite behind it inherits an interrupt none of those tests was ever sent. The half-built entry
is dropped instead, and anyone already parked on its future is handed an internal retry marker
(`_ConstructionCancelled`) that sends them back around `acquire`'s loop to build it for real. That
marker never escapes `acquire`, which is the only code that awaits an entry's future.

**Waiters await the entry's future through an `asyncio.shield`.** An entry's future is shared by
everyone asking for that key, and awaiting it bare makes it the awaiting *task*'s own
`_fut_waiter` — which `Task.cancel` cancels directly. One waiter's `@voci.timeout` expiring would
therefore cancel the construction out from under the constructor (whose `set_result` then raises
`InvalidStateError`) and every other waiter alongside it, from a deadline none of them was given.
The shield gives each caller a private future to be cancelled instead, so a cancellation reaches
exactly the requester it was aimed at.

**Teardown order relies on a precondition that lives outside the class.** `aclose` tears down in
reverse insertion order, which is only valid because `setup` walks an already-topologically-sorted
plan forwards and `build()` never calls back into `acquire`. Every dependency is therefore inserted
before its dependent. Any future feature where a fixture resolves a dependency lazily *during* its
own construction breaks this without changing a line of `aclose` — whoever adds one has to revisit
this method.

**`"call"` scope ranks equal to `"function"`, not below it.** The compatibility check cares about
lifetime, and both tear down at end of test. Ranking `"call"` lower would reject valid graphs over a
caching distinction the check was never meant to police.

**A parametrized fixture's case value has no `request` object to travel through.** voci never
grows one (`_check_missing_injections` has no name-based fallback to hang it off), so `params=`
reuses the convention `@voci.parametrize` already established: the value arrives as an ordinary
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
