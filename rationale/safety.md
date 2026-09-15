# `_run/safety.py` — the loop watchdog and tests that check nothing

See [rationale.md](../rationale.md) for the index.

**The watchdog reports from a thread, because the loop is the thing that cannot report.** A
blocking call inside an `async def` test holds the one loop every concurrently dispatched test
shares, and from the loop's own perspective nothing is happening at all — no callback can run to
notice, which is why an unwatched stall reads as voci hanging rather than as a test misbehaving.
A daemon thread reading a heartbeat the loop leaves behind is the only vantage point that still
works while the loop is held, and `sys._current_frames()` is what turns "something is stuck" into
a file, a line, and a test id. It warns and never fails a test: voci cannot tell a blocking call
apart from a fixture that legitimately takes a while, and a diagnostic that can be wrong must not
be able to fail a build.

**A reported stack is cut at voci's own innermost frame.** Everything outside it is voci
dispatching a test and asyncio dispatching voci — the same dozen frames on every report, none of
which answer the question. Everything inside is the test's own call chain, which does.

**Un-awaited coroutines ride CPython's own warning rather than a coroutine tracker.** The
interpreter already reports every coroutine whose last reference goes away un-started, at the
moment it goes away — which for one a test created is inside that test's own call phase. Reading
which test is running off a `ContextVar` when that warning arrives is free; tracking coroutine
creation instead would mean a `sys.setprofile`-class hook on the hot path of every test in the
suite to catch a mistake that, once caught, is a one-line fix. The trade is that a coroutine
deliberately kept alive past the end of the test that made it is filed against whoever is running
when it is finally collected. This is a hook registered with `_warnings.py`'s shim rather than a
second `showwarning` of its own, and it claims the warning outright: the test's own failure says
it better, and no user filter should be able to silence it.

**A sync test stuck in a worker thread is named, not waited for.** Nothing in Python can interrupt
a thread: cancelling the future that awaits one abandons the wait, not the call. So the executor
never joins on shutdown — waiting there would make the end of a run, and a Ctrl-C especially, take
exactly as long as the blocking call that made the run worth abandoning — and voci instead
prints which test is still running and where it is blocked. The interpreter still cannot exit
until that call returns; the difference is whether the user is told why.

**A test that returns a value or drops a coroutine fails regardless of `@voci.xfail`.** `xfail`
re-reads what the call phase *raised*. Neither of these raises anything: they are a test that ran
to the end while checking nothing, which no mark can have predicted and no `raises=` can match.
