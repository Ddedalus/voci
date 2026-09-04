# Tuning concurrency

`--concurrency` caps how many tests run at once; the default is 16. Whether that number is doing
anything for a given suite is a question `--durations` answers, not a guess:

```console
$ velox --durations 5
...
34 tests · 33 passed · 1 skipped · 0.15s wall (0.6x concurrency)
```

The multiplier at the end is the sum of every test's own duration divided by the wall time the run
actually took. Above 1.0, tests overlapped; at or below 1.0, they didn't — either the suite is too
fast for scheduling to matter, or something is holding it to one test at a time regardless of
`--concurrency`.

## Reading what `--durations` names

`--durations N` lists the N slowest tests at the end of the run — under concurrency, the slowest
test is the floor the wall clock can't drop below, so it's the list to read before touching
`--concurrency` at all. A test that patches a module or a class installs by mutating something
every other running test can see, so velox schedules it to run alone and drains the suite around
it:

```python
@mock.patch("relay.client.random.uniform", return_value=1.0)
async def test_jitter_is_deterministic(
    uniform: mock.MagicMock,
    r: Relay = Depends(flaky_relay),
    t: FakeTransport = Depends(flaky_transport),
) -> None:
    """`relay.client.random.uniform` is a module global, so this test is scheduled to run alone
    and the suite drains around it — the summary line at the end of the run says how long that
    took.
    """
    ...
```

A suite with several of these shows up as a low multiplier and a `--durations` list topped by
tests that, individually, aren't slow — they're just serialized against everything else. The fix
is rarely `--concurrency`; it's removing the reason a test needs to run alone. Here, that's an
injectable `jitter: Callable[[], float]` argument on `Relay` in place of patching
`random.uniform` — dependency injection runs fully concurrently, because nothing about it is a
process-global write:

```python
async def test_transport_substituted_by_di(
    r: Relay = Depends(flaky_relay),
    t: FakeTransport = Depends(flaky_transport),
) -> None:
    """No patching at all. Runs alongside every other test in the suite."""
    await r.deliver("https://hooks.test/v1", b"x")
    assert len(t.sent) == 3
```

A run's footer breaks out solo time on its own line when any test ran that way:

```console
unittest.mock: 2 tests ran solo · Σ 0.00s of 0.15s wall
```

## Setting `--concurrency`

Once solo-scheduled tests are accounted for, `--concurrency` is what's left to tune. Raising it
helps a suite bottlenecked on waiting — I/O, sleeps, network calls — up to whatever the underlying
resource (a connection pool, a rate limit, the number of cores available to sync tests) can
actually sustain; past that point the multiplier stops climbing and only memory and contention go
up. `--concurrency 1` (or `--serial`) is the floor, useful for isolating a single test's true cost
rather than tuning a whole suite — see [Debugging a flaky test](debugging-a-flaky-test.md).

The full mocking ladder is in `examples/02-async-library/tests/test_patching.py`.
