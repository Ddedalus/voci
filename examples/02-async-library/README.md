# 02 — A pure-async library, and the mocking ladder

Standalone: stdlib only, no `requirements.txt`. A webhook delivery client with retry, jittered
backoff, and a TTL cache.

```
relay/
  transport.py    Transport protocol + FakeTransport   <- the seam
  cache.py        TTLCache with an injectable clock     <- the seam
  client.py       Relay: retries, and `random.uniform` inline  <- NOT a seam
  settings.py     Settings.from_env(env)                <- the seam
tests/
  fixtures.py
  test_delivery.py   retries, logs, timeout, sync tests
  test_cache.py      parametrize, ids, approx, xfail(raises=)
  test_patching.py   the ladder, cheapest rung first
```

## Setup

```bash
uv venv && uv pip install -e ../..
```

## Commands

```bash
velox
velox -v                          # per-test lines, and which tier each patching test used
velox tests/test_patching.py      # the interesting file
velox -k "not patch"              # skip the solo ones and watch the wall clock drop
velox --concurrency 1             # everything serial: solo costs nothing, and shows nothing
```

## Expected output

```
velox 0.1.0 · python 3.13.2 · uvloop · concurrency 16 · seed 0 · assert=rewrite

PASS  tests/test_cache.py                  15 tests   0.09s
PASS  tests/test_delivery.py               16 tests   0.42s
PASS  tests/test_patching.py                7 tests   0.31s

  3 tests ran solo · suite drained for 0.21s (38% of wall clock)
      relay.client.random.uniform      2 tests
      os.environ                       1 test

38 tests · 37 passed · 1 xfailed · 0.55s wall (Σ 1.9s, 3.5× concurrency)
```

That solo block is the feature. Three tests out of thirty-eight held the suite's write lock for
38% of the run. Nothing is hidden and nothing is estimated — a suite that drifts into two hundred
solo tests has lost its parallelism, and it should find that out from the summary rather than from
a stopwatch six months later.

## The ladder

`tests/test_patching.py` walks it in order.

| Tier | Mechanism | Concurrency | In this suite |
|---|---|---|---|
| **(a)** | Dependency injection — a sibling fixture, a constructor argument, a `Protocol` | Full | 4 tests |
| **(a)** | `MagicMock` / `create_autospec` as a *value* | Full | 1 test |
| **(b)** | `@mock.patch(...)` — detected statically, scheduled solo | Suite drains | 2 tests |
| **(b)** | `with mock.patch(...)` — invisible statically, must be marked `@velox.solo` | Suite drains | 1 test |
| **(d)** | `@velox.isolated` — subprocess, fresh loop | Full, minus a spawn | 1 test *(roadmap)* |

Two things are worth stating plainly, because they are the most common misreadings:

**Mock objects were never the problem.** `MagicMock`, `AsyncMock`, `create_autospec`,
`assert_awaited_once_with` — all per-instance state, no global writes, perfectly safe under
concurrency. velox does not wrap, replace, or discourage any of it. The problem is exclusively the
*installer*: `mock.patch` performs a real `setattr` on a module or class, which every running test
sees.

**velox therefore ships no patch API of its own.** A `velox.patch` that always ran solo would be
`unittest.mock.patch` with a different import line — same semantics, same speed, plus a migration
cost. The name is reserved for the routing tier (spec/08 §5), where it would do something
`mock.patch` structurally *cannot*: let two concurrent tests patch the same target differently.
Until that exists, the answer is "use `unittest.mock`, and velox will schedule it safely" — which
also means every existing `mock.patch` call site in a migrating suite keeps working untouched.

## Detection, concretely

- **Decorator form** costs one attribute lookup at collection. `unittest.mock`'s
  `_patch.decorate_callable` sets `func.patchings = [self]` on the wrapper it returns (and appends
  for stacked patches), so `hasattr(func, "patchings")` finds every decorated test, and reading
  `p.target` off each patcher is what fills in the "relay.client.random.uniform — 2 tests" line
  above.
- **Context-manager form** cannot be seen from the function object; the patcher does not exist
  until the `with` line executes. velox wraps `unittest.mock._patch.__enter__` so an unmarked one
  **fails with an actionable message** instead of silently racing, and `velox migrate` adds the
  `@velox.solo` decorator statically so a migrated suite is annotated before it first runs.

## The point of the file layout

`relay/cache.py` and `relay/client.py` are the same code with one difference: the cache takes its
clock as a parameter and the client does not.

That difference is worth 38% of this suite's wall clock. The cache's time-travel tests run sixteen
wide; the client's jitter test stops the world. No mocking library can close that gap — it is a
property of the code under test, and the only fix is a constructor argument.

This is the argument velox is really making. Explicit dependency injection is not overhead you pay
for the framework's benefit; it is the thing that makes your suite parallelisable, and velox's
reporting is arranged to keep showing you the bill until you take the seam.
