# 02 — A pure-async library, and the mocking ladder

Standalone: stdlib only, no `requirements.txt`. A webhook delivery client with retry, jittered
backoff, and a TTL cache. `tests/test_patching.py` walks the mocking ladder cheapest rung first:
`unittest.mock` objects are per-instance state and run fully concurrently, but `mock.patch` installs
by mutating a module or class, so tests using it are marked `skip` rather than run unguarded
against each other's shared target.

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

`pyproject.toml` sets `extend-immutable-calls = ["velox.Depends"]` under
`[tool.ruff.lint.flake8-bugbear]`, without which ruff's B008 fires on every `Depends(...)` default.

## Commands

```bash
velox                          # everything (28 tests, 4 skipped)
velox tests/test_patching.py   # the interesting file
velox --concurrency 1          # exactly serial
velox --timeout 5              # per-test setup+call budget
```

## Expected output

```
$ velox
assertions: rewrite, cache /home/you/.cache/velox/rewrite
config: /path/to/examples/02-async-library/pyproject.toml
PASS  tests/test_cache.py                12 tests   Σ 0.00s
PASS  tests/test_patching.py              5 tests   Σ 0.01s
PASS  tests/test_delivery.py             11 tests   Σ 0.07s
tests/test_cache.py::test_evicts_when_full SKIPPED (cache does not evict on size yet ...)
tests/test_patching.py::test_context_manager_patching_must_be_marked SKIPPED (patches the same target as ...)
tests/test_patching.py::test_relative_path_resolution SKIPPED (os.chdir has process-wide effect)
tests/test_patching.py::test_jitter_is_deterministic SKIPPED (relay.client.random.uniform is a module-global function ...)
28 tests: 28 passed, 0 failed, 0 errored, 4 skipped, 0 collection error(s)

28 tests · 0 failed · 0.06s wall (Σ 0.07s, 1.3x concurrency)
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
  `test_jitter_is_deterministic` shows what patching it costs: the test is marked `skip` because a
  decorator-installed patch on a module-global name would collide with every other concurrently
  running test that exercises the same retry path.
- **`tests/fixtures.py::audit_log`** — a session-scoped fixture built once under a single-flight
  guard and torn down by refcount after the last dependent test finishes.
