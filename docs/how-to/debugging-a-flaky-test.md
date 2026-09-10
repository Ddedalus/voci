# Debugging a flaky test

A test that fails only sometimes, or only alongside the rest of the suite, is usually a
concurrency problem. Three flags narrow it down, cheapest first.

## `--serial`

`--serial` is shorthand for `--concurrency 1`: one test at a time, in collection order. If a test
fails under `voci` but passes under `voci --serial <same test id>`, the cause is something it
shares with whatever else was running, not a bug in the test itself.

```python
async def test_blocking_call_stalls_the_loop(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    """`balance_blocking` is a plain synchronous method that, called directly from a coroutine,
    runs on the event loop thread: every other in-flight test is frozen for as long as sqlite is
    busy, with no timeout and no exception — the only symptom is that the suite got slower.
    """
    await svc.append(acct, 999)

    blocking = svc.balance_blocking(acct)  # runs on the event loop thread
    correct = await asyncio.to_thread(svc.balance_blocking, acct)  # off the event loop thread

    assert blocking == correct == 999
```

This test always passes on its own — `blocking` and `correct` agree either way. What `--serial`
rules in or out is everything *else* it does while it runs: a blocking call on the event loop
raises no exception and times out nothing, so a neighbour timing out, or simply running slower
than its own budget suggests, is the only visible symptom.

## `--loop-watchdog`

`--loop-watchdog SECONDS` (default 5.0) warns when the event loop has been blocked that long,
naming the call holding it. A blocking call in one test's fixture or body stalls every task
sharing that loop, so this flag turns "the suite got slower for no reason" into a specific stack:

```
voci: the event loop has been blocked for 6.3s
  tests/test_safety.py::test_blocking_call_stalls_the_loop is holding it; nothing else runs
  until it returns (11 tests in flight):
    File "ledger/store.py", line 42, in balance
      ...
  A blocking call in an `async def` test, or in a fixture, holds the loop for the whole suite.
  Move the work into a sync `def` test (voci runs those in a worker thread), or
  await asyncio.to_thread(...).
```

`test_blocking_call_stalls_the_loop` above calls `balance_blocking` against a local, near-empty
SQLite file — far too quick to trip even a lowered threshold. Against a real, loaded database, the
same call produces the warning above; the fix either way is the one the test already shows,
`asyncio.to_thread(...)`. Lower `--loop-watchdog` below the default when chasing a stall shorter
than 5 seconds; pass `0` to turn it off once you've found the call and don't want the warning on
every run.

## `-x` / `--maxfail`

`-x` stops the run at the first failure — tests not yet started are dropped, and any still in
flight are cancelled and reported `CANCELLED` rather than left to finish. Combined with `--serial`,
it's the fastest way to land on a single reproducible failure instead of reading through a run
where several things went wrong for different reasons:

```console
$ voci --serial -x tests/test_safety.py
```

`--maxfail N` generalizes it past the first failure, useful once `-x` has confirmed a test is
flaky and you want to see whether it fails once out of a batch of retries or every time.

The full test is in `examples/03-shared-resources/tests/test_safety.py`, alongside the fix —
`LedgerService.balance` wraps the same call in `asyncio.to_thread` instead of calling it directly.
