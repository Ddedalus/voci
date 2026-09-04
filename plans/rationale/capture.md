# `_builtins/capture.py` — capture and routing

See [rationale.md](../rationale.md) for the index.

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
