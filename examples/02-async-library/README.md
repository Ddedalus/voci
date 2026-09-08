# 02 — A pure-async library, and the mocking ladder

Standalone: stdlib only, no `requirements.txt`. A webhook delivery client with retry, jittered
backoff, and a TTL cache. `tests/test_patching.py` walks the mocking ladder cheapest rung first:
`unittest.mock` objects are per-instance state and run fully concurrently, while `mock.patch`
installs by mutating a module or class, so voci finds those tests at collection and runs them
alone — the last lines of the run say how much of the wall clock that cost.

```
relay/
  transport.py    Transport protocol + FakeTransport   <- the seam
  cache.py        TTLCache with an injectable clock     <- the seam
  client.py       Relay: retries, and `random.uniform` inline  <- NOT a seam
  settings.py     Settings.from_env(env)                <- the seam
tests/
  fixtures.py
  test_delivery.py   retries, logs, timeout
  test_cache.py      ids, approx, and a size-eviction gap tracked with skip
  test_patching.py   the mocking ladder, cheapest rung first
```

## Setup

```bash
uv venv && uv pip install -e ../..
```

## Commands

```bash
voci                          # everything (34 tests, 1 skipped)
voci tests/test_patching.py   # the interesting file
voci --concurrency 1          # exactly serial
voci --timeout 5              # per-test setup+call budget
```

## Expected output

```
$ voci
assertions: rewrite, cache /home/you/.cache/voci/rewrite
config: /path/to/examples/02-async-library/pyproject.toml
PASS  tests/test_cache.py                        13 tests  Σ 0.00s   (1 skipped)
PASS  tests/test_delivery.py                     13 tests  Σ 0.08s
PASS  tests/test_patching.py                      8 tests  Σ 0.01s

unittest.mock: 2 tests ran solo · Σ 0.00s of 0.15s wall

34 tests · 33 passed · 1 skipped · 0.15s wall (0.6x concurrency)
```

## What to look at

- **`relay/cache.py`'s `TTLCache`** takes its clock as a constructor argument, so
  `test_clock_substituted_by_di` in `test_patching.py` advances time with a method call — no
  patching, no `freezegun`, fully concurrent.
- **`relay/settings.py`'s `Settings.from_env`** takes the environment mapping as a parameter, so
  `test_settings_substituted_by_di` needs no `monkeypatch.setenv` or `mock.patch.dict`.
- **`test_patching.py::test_transport_substituted_by_a_mock_object`** — a `MagicMock`/
  `create_autospec` used as a value, not installed anywhere; safe under concurrency with no
  scheduling cost at all.
- **`relay/client.py`'s inline `random.uniform`** is the one seam this library doesn't have, and
  `test_jitter_is_deterministic` shows what patching it costs: voci finds the `@mock.patch`
  decorator at collection, injects the test's fixtures around the mock parameter, and drains the
  suite to run it alone — otherwise every concurrently running test through the same retry path
  would see the patch too.
- **`test_patching.py::test_context_manager_patching_must_be_marked`** — the same patch written as
  `with mock.patch(...)`, which nothing can find until the line runs. `@voci.solo` is written by
  hand; without it voci refuses the patch as it installs and the test fails.
- **`tests/fixtures.py::audit_log`** — a session-scoped fixture built once under a single-flight
  guard and torn down by refcount after the last dependent test finishes.
