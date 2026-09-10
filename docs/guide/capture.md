# Capture

Every test's stdout, stderr and log records are captured by default, and shown in the report only
if that test fails:

```console
FAILED tests/test_delivery.py::test_retries_are_logged
...
--- captured stdout ---
retrying after connection reset

--- captured log records ---
WARNING  relay:client.py:82 attempt 1/3 failed: connection reset
```

`--capture no` (or `-s`) turns this off for a live pass-through instead, each line prefixed with
the test id it came from — several tests can be printing at once.

## Reading captured output from inside a test

`voci.capture` hands back a live view of the current test's stdout/stderr, for asserting on
output directly rather than waiting for a failure to show it:

```python
async def test_warns_on_missing_config(
    c: Annotated[voci.Capture, Depends(voci.capture)],
) -> None:
    load_config(path=None)
    assert "no config path given" in c.err
```

`.out`/`.err` re-read the capture buffer on every access, so text written after the fixture was
injected is visible immediately — nothing about it needs to be re-fetched.

## Reading captured log records

`voci.log_records` is the same idea for `logging`, with `.records` as raw `logging.LogRecord`s,
`.messages` as their formatted text, and `.record_tuples` as `(logger name, level, message)` for
assertion comparison:

```python
async def test_retries_are_logged(
    r: Annotated[Relay, Depends(flaky_relay)],
    logs: Annotated[voci.LogRecords, Depends(voci.log_records)],
) -> None:
    with logs.set_level(logging.WARNING, logger="relay"):
        await r.deliver("https://hooks.test/v1", b"retry-me")

    warnings = [rec for rec in logs.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "attempt 1/3" in logs.messages[0]
```

`set_level` raises or lowers a logger's level for the block and restores it after, `None`
targeting the root logger. A logger's level is process state, shared by every test — raising it
inside `set_level` lets a concurrently-running test's own logging through too, and lowering one
can leave a neighbour's own `set_level` block emptier than it expected.

Both `capture` and `log_records` are attributed by an `asyncio` `ContextVar`, not by swapping a
global stream or handler: many tests logging or printing at once each see only their own output,
including anything written from inside `asyncio.to_thread`, which inherits the context that set
it up.

## Temporary directories

`voci.tmp_path` is a `pathlib.Path` unique to the running test, built from its own id, so nothing
about naming it needs a scan-and-retry:

```python
async def test_stats_can_be_dumped(
    c: Annotated[TTLCache, Depends(cache)],
    tmp: Annotated[Path, Depends(voci.tmp_path)],
) -> None:
    c.put("a", b"1")
    target = tmp / "stats.json"
    target.write_text(json.dumps({"hits": c.hits}))
    assert json.loads(target.read_text()) == {"hits": 1}
```

Every `tmp_path` lives under one root per run, a fresh numbered directory in the platform temp
directory; `--basetemp` moves that root, and the last three runs' directories are kept alongside
it for a post-mortem. `voci.tmp_path_factory` is the session-scoped fixture behind it, for a
fixture that needs its own directory rather than the one the test itself gets.
