# `_mocking.py` — `unittest.mock` patching

See [rationale.md](../rationale.md) for the index.

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
