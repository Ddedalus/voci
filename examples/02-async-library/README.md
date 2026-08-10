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
  test_cache.py      ids, approx, and a size-eviction gap tracked with skip
  test_patching.py   the ladder, cheapest rung first — see "Known gaps" below
```

## Setup

```bash
uv venv && uv pip install -e ../..
```

## Commands

This is the current, working CLI surface, not the eventual one — see
[`examples/01-fastapi-crud`'s README](../01-fastapi-crud/README.md#commands) for the full list of
what's still missing (`-k`/`-m`, `-v`/`-q`, `::` test-id addressing, `--durations`,
`--collect-only`, `--serial`).

```bash
velox                          # everything (28 tests, 4 skipped)
velox tests/test_patching.py   # the interesting file
velox --concurrency 1          # exactly serial
velox --concurrency 64
velox --timeout 5
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
tests/test_patching.py::test_context_manager_patching_must_be_marked SKIPPED (would race ...)
tests/test_patching.py::test_relative_path_resolution SKIPPED (@velox.isolated has no ...)
tests/test_patching.py::test_jitter_is_deterministic SKIPPED (would run unmarked and ...)
28 tests: 28 passed, 0 failed, 0 errored, 4 skipped, 0 collection error(s)

28 tests · 0 failed · 0.06s wall (Σ 0.07s, 1.3x concurrency)
```

No solo write-lock block, no wall-clock cost from patching, and no `xfailed` count — see "Known
gaps" below for why: the four `SKIPPED` lines are exactly the tests that would have demonstrated
those things, held back rather than run in a way that would be flaky or actively unsafe today.

## The ladder

`tests/test_patching.py` walks it in order. "Live" means dispatched and asserted on every run;
"skipped" means the code is there to read but `velox` doesn't execute it (see "Known gaps").

| Tier | Mechanism | Concurrency | In this suite | Status |
|---|---|---|---|---|
| **(a)** | Dependency injection — a sibling fixture, a constructor argument, a `Protocol` | Full | 4 tests | Live |
| **(a)** | `MagicMock` / `create_autospec` as a *value* | Full | 1 test | Live |
| **(b)** | `@mock.patch(...)` — would be detected statically, scheduled solo | Suite drains | 2 tests | 1 live¹, 1 skipped |
| **(b)** | `with mock.patch(...)` — invisible statically, must be marked `@velox.solo` | Suite drains | 1 test | Skipped |
| **(d)** | `@velox.isolated` — subprocess, fresh loop | Full, minus a spawn | 1 test | Skipped *(roadmap)* |

¹ `test_settings_from_the_real_environment` (`mock.patch.dict(os.environ, ...)`) is live because
nothing else in this suite reads the real `os.environ` — there's no live neighbour for it to race.
`test_jitter_is_deterministic` (`mock.patch("relay.client.random.uniform", ...)`) is *not* live:
`Relay.deliver`'s retry path is exercised by several other tests in this suite, and a decorator
patch is a real, process-global `setattr` for its whole duration — every one of those concurrently
running would call the same patched name. See its own `@velox.skip` reason for how that was
verified, not just asserted.

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
also means every existing `mock.patch` call site in a migrating suite keeps working untouched, once
that scheduling actually lands (see "Known gaps").

## Detection, concretely (design, not yet built — spec/08)

- **Decorator form** would cost one attribute lookup at collection. `unittest.mock`'s
  `_patch.decorate_callable` sets `func.patchings = [self]` on the wrapper it returns (and appends
  for stacked patches), so `hasattr(func, "patchings")` finds every decorated test, and reading
  `p.target` off each patcher is what would fill in a "relay.client.random.uniform — N tests" line
  in the report.
- **Context-manager form** cannot be seen from the function object; the patcher does not exist
  until the `with` line executes. The plan is for velox to wrap `unittest.mock._patch.__enter__` so
  an unmarked one **fails with an actionable message** instead of silently racing, and for
  `velox migrate` to add the `@velox.solo` decorator statically so a migrated suite is annotated
  before it first runs.

## Known gaps (tracked, not bugs in this suite)

Dogfooding this example against the current runner surfaced the same class of thing
[`examples/01-fastapi-crud`'s README](../01-fastapi-crud/README.md#known-gaps-tracked-not-bugs-in-this-suite)
did — public API that's declared but not yet acted on — plus one specific to this example's subject
(patching detection), all tracked in [`docs/M1-PLAN.md`](../../docs/M1-PLAN.md):

- **`@velox.solo`/`@velox.isolated` aren't enforced.** Both marks are recorded on a function's
  marks the same way `skip`/`skipif` are, but `_run.py`'s own module docstring says so directly:
  "exclusive-resource admission, the solo write-lock tier, aging... still deliberately not built
  this slice." A test marked `@velox.solo` runs exactly like an unmarked one — fully concurrent,
  no suite-wide lock — which is unsafe, not just unfinished, for a test that does a real global
  `setattr`. `test_context_manager_patching_must_be_marked` is skipped rather than left to race.
- **`@mock.patch`-decorated tests don't just skip solo scheduling — DI silently no-ops for them.**
  `_fixtures.plan_for` reads `func.__code__`/`func.__defaults__` directly (deliberately never
  `inspect.signature`/`__wrapped__` — see `_fixtures.py`'s own module docstring), so a
  `@mock.patch(...)`-wrapped test presents as `(*args, **keywargs)`: zero named parameters, zero
  `Depends()` defaults found. Any `Depends(...)` default on the *real* function underneath is
  never resolved — the raw, unresolved `Depends(...)` object is what the parameter gets, since
  velox ends up calling the wrapper with no arguments at all and Python falls back to the
  function's own literal default. `test_jitter_is_deterministic` sidesteps this by building its
  `Relay`/`FakeTransport` by hand instead of injecting them.
- **`@velox.xfail` execution and `@velox.parametrize` expansion** — same gaps as example 01,
  found again here: `test_evicts_when_full` uses `skip` in place of `xfail(strict=True,
  raises=AssertionError)`, and `test_expiry_boundary_*`/`test_ttl_is_respected_for_*`/
  `test_2xx_4xx_is_never_retried`-shaped tests are parametrize cases written out by hand.
- **`.records[i].message` is unset** on a `velox.log_records` `LogRecord` — same trap as example
  01's `test_duplicate_email_is_logged`, hit again in `test_retries_are_logged` and fixed the same
  way (`logs.messages`, index-aligned with `logs.records`). Documented in
  [spec/09 §2](../../spec/09-capture-and-logging.md).

None of these block a green run — `velox` with no arguments passes end to end (see "Expected
output" above) — but they're why four tests in `test_patching.py` are marked `skip` instead of
demonstrating solo/isolated scheduling live: running them for real today would either be flaky (two
`mock.patch` calls racing the same global) or actively unsafe (`os.chdir` with no subprocess to
contain it).

## The point of the file layout

`relay/cache.py` and `relay/client.py` are the same code with one difference: the cache takes its
clock as a parameter and the client does not.

Once solo/isolated scheduling exists, that difference is what will show up as wall-clock cost in
the report — the cache's time-travel tests running sixteen wide, the client's jitter test stopping
the world for everyone. Today it shows up differently: as the four `SKIPPED` lines above, since
running the client's version live isn't safe yet either way. No mocking library closes that gap —
it is a property of the code under test, and the only fix is a constructor argument.

This is the argument velox is really making. Explicit dependency injection is not overhead you pay
for the framework's benefit; it is the thing that makes your suite parallelisable, and it is also,
right now, the thing standing between "this test runs" and "this test is marked skip and waits for
a scheduler."
