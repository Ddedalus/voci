# Concurrency

voci dispatches every collected test as a concurrent task, admitted through a shared gate that
bounds how many run at once to `--concurrency` — 16 by default, high enough that most suites are
limited by a downstream service's latency rather than by this cap. Each admitted test still gets
its own full setup, call, and teardown; only how many run at the same moment is bounded.

`--concurrency` takes a positive integer. Lower it to hold a suite back from a resource it would
otherwise overwhelm — a connection pool, a rate limit; raise it for a suite that's bottlenecked on
waiting rather than CPU. `--concurrency=1`, or its shorthand `--serial`, is the floor: one test at a
time, in collection order.

```console
$ voci --concurrency 4
```

`[tool.voci] concurrency` sets the same value from config; a `--concurrency` typed on the command
line outranks it.

## The timeout budget

`--timeout` puts a budget, in seconds, on each test's setup+call phase. There is no limit by
default. A test that exceeds its budget is reported `TIMEOUT`, not `FAILED` or `ERROR`.

`@voci.timeout(seconds)` overrides the suite-wide budget for one test:

```python
@voci.timeout(0.5)
async def test_responds_promptly(client: Annotated[AsyncClient, Depends(api_client)]) -> None:
    await client.get("/health")
```

This test gets 0.5 seconds regardless of what `--timeout` sets for the rest of the suite —
`[tool.voci] timeout` sets that suite-wide default from config the same way `concurrency` does.

Once the admission and timeout model here makes sense, [Tuning concurrency](../how-to/tuning-concurrency.md)
covers reading `--durations` and working out why a run's wall time isn't reflecting
`--concurrency`.
