# 01 — FastAPI + async SQLAlchemy

The reference stack. A small CRUD API tested end to end through `httpx.AsyncClient` against a real
database, with a transaction-per-test that is always rolled back.

```
app/
  settings.py     frozen dataclass, injectable, reads env through a parameter
  models.py       User, Order
  db.py           get_session — the seam tests override
  main.py         routes + the module-level `app = FastAPI(...)`, written for production only
tests/
  fixtures.py     engine, session, api_client, alice, payment_sandbox
  test_users.py   the baseline shape: fixtures, skip/skipif, a timeout override
  test_orders.py  per-node override via a sibling fixture, raises/approx, built-ins, exclusive
```

## Setup

```bash
uv venv
uv pip install -r requirements.txt -e ../..
```

## Commands

This is the current, working CLI surface — not the eventual one. `-k`/`-m` selection, `-v`/`-q`
verbosity, `--durations`, `--collect-only`, `--serial`, and `path.py::test_name` id addressing are
all real, spec'd invocation syntax (spec/02 §1) that M1 hasn't wired up yet; see
[`docs/M1-PLAN.md`](../../docs/M1-PLAN.md) for what's tracked where. What's below runs today.

```bash
velox                          # everything (22 tests), up to 16 at once ([tool.velox] concurrency)
velox tests/test_users.py      # one file (velox tests/test_users.py::test_health -- not yet: `::`
                                # id addressing is M2, see M1-PLAN.md's "Test-id selection" item)

velox --concurrency 1          # exactly serial -- the first debugging step
velox --concurrency 64         # if the database can take it
velox --timeout 5              # per-test setup+call budget; TIMEOUT, not FAILED, past it
velox -s                       # (or --capture=no) live, id-prefixed stdout/stderr instead of
                                # captured-and-shown-only-on-failure
velox --assert plain           # skip the rewrite import hook; PEP 657 caret fallback still applies
```

## Expected output

```
$ velox
assertions: rewrite, cache /home/you/.cache/velox/rewrite
config: /path/to/examples/01-fastapi-crud/pyproject.toml
PASS  tests/test_users.py                10 tests   Σ 3.84s
PASS  tests/test_orders.py               12 tests   Σ 7.29s
tests/test_users.py::test_list_users_is_paginated SKIPPED (pagination is not implemented yet (GET /users has no route -- 405, not 200))
tests/test_users.py::test_response_carries_request_id SKIPPED (middleware is behind a feature flag)
22 tests: 22 passed, 0 failed, 0 errored, 2 skipped, 0 collection error(s)

22 tests · 0 failed · 1.14s wall (Σ 11.13s, 9.8x concurrency)
```

The last line is the one to watch. `Σ 11.13s` is the serial cost of the 22 dispatched tests; `1.14s
wall` is what you actually waited. `assertions:`/`config:` above it are printed unconditionally
(spec/02 §5, spec/07 §5.3) so a run never picks up rewrite-cache or `[tool.velox]` behavior you
didn't know was there. The two `SKIPPED` lines are `skip`/`skipif`, and are excluded from the "22
tests" dispatched count — they never ran (see `test_users.py` for why each is skipped, and for the
still-declared-but-not-yet-enforced `@velox.xfail` this one used to be).

A failing run (`tests/test_orders.py::test_order_limit_is_enforced`, provoked here by editing its
own assertion — see its source for what it actually checks):

```
assertions: rewrite, cache /home/you/.cache/velox/rewrite
config: /path/to/examples/01-fastapi-crud/pyproject.toml
PASS  tests/test_users.py                10 tests   Σ 5.25s
FAIL  tests/test_orders.py               12 tests   Σ 9.04s   (1 failed)
tests/test_users.py::test_list_users_is_paginated SKIPPED (pagination is not implemented yet (GET /users has no route -- 405, not 200))
tests/test_users.py::test_response_carries_request_id SKIPPED (middleware is behind a feature flag)
22 tests: 21 passed, 1 failed, 0 errored, 2 skipped, 0 collection error(s)

FAILED tests/test_orders.py::test_order_limit_is_enforced
Traceback (most recent call last):
  File ".../velox/_run.py", line 294, in _run_one
    await coro
  File ".../tests/test_orders.py", line 134, in test_order_limit_is_enforced
    assert limited.status_code == 430
AssertionError: assert 429 == 430
 +  where 429 = <Response [429 Too Many Requests]>.status_code
--- short test summary ---
FAILED tests/test_orders.py::test_order_limit_is_enforced - AssertionError: assert 429 == 430

22 tests · 1 failed · 1.36s wall (Σ 14.29s, 10.5x concurrency)
```

Failure detail is printed at the end in **logical** (collection) order, never in completion order,
so the output is byte-identical run to run no matter how the tasks interleaved.

## What to look at

**`tests/fixtures.py::engine`** — one engine for the whole run, shared by every concurrent test.
This is the head-to-head argument against `xdist`: `-n 4` gives you four processes, four engines,
four pools, and four copies of every import, and R§1 measured it *slower* than serial on the suite
it was tried on. Here the concurrency is inside one process, so the expensive thing is built once.

**`tests/fixtures.py::session`** — the transaction that is always rolled back. Every test gets the
real schema and sees none of its neighbours' writes. This is what makes concurrency safe for a
database suite without giving each test its own database.

**`tests/fixtures.py::api_client`** — *your* app. `app/main.py` ends with a module-level
`app = FastAPI(lifespan=lifespan)`, the way it would be written if this suite did not exist, and
the tests use that object. No factory, no per-test rebuild, no production code shaped by its tests.

The override itself is the line from the FastAPI docs, unchanged:
`{get_session: lambda: session}`. What velox adds is where it is *stored*.
`app.dependency_overrides` and `app.state` are per-app-instance dicts, so sixteen concurrent tests
writing them are sixteen tests writing one dict — and the `dependency_overrides.clear()` those docs
put in teardown wipes it out from under the fifteen still in flight. `velox.fastapi.client()` swaps
each attribute, once, for a proxy that layers a `ContextVar` over the original, so every test reads
and writes its own layer through the same singleton. Collisions are not possible, and the teardown
line is not needed: the layer is dropped when the fixture's `async with` exits, on the failure path
as on the success path.

The app's `lifespan` never runs under the test client — `httpx.ASGITransport` sends no lifespan
scope — so no real engine is built and `app.state.sessionmaker` is never set. Nothing reads it,
because `get_session` is overridden. For an app whose startup puts something under test in place,
`velox.fastapi.lifespan(app)` is a session-scoped fixture that runs it once for the whole run.

**`test_orders.py::test_premium_signup_grants_credit`** — `premium_client` is a sibling fixture
that rebuilds `api_client` with `premium_settings` wired in by hand, replacing one node of the
graph for one test. No patching, no override registry, no teardown; the substitution is an
ordinary fixture in the static graph, so validation and scheduling still see the truth.
Deriving `premium_client` from `api_client` automatically — `api_client.with_(settings=...)` — is
roadmap ([spec/01 §10](../../spec/01-public-api.md)): it has a caching-identity problem that
needs a design decision, not a patch.

**`tests/fixtures.py::payment_sandbox`** — `exclusive="payments-sandbox"`. The constraint is declared
on the *resource*; every test that transitively depends on it inherits the token and the scheduler
does the rest. No test has to remember to annotate itself, so no test can forget.

## Known gaps (tracked, not bugs in this suite)

Dogfooding this example against the current runner surfaced four things declared in velox's public
API that the runner doesn't act on yet — all tracked in
[`docs/M1-PLAN.md`](../../docs/M1-PLAN.md) "Tracked but not blocking the gate":

- **`@velox.parametrize`** expands to a `ParamSet` mark but `_collect.collect` doesn't turn it into
  one `TestRecord` per case — applying it produces a `DIError` ("parameter(s) ... have no default
  and are not injected"), not multiple passing tests. `test_users.py`/`test_orders.py` spell the
  cases out as separate functions instead; see `test_create_user_bob` and its neighbours.
- **`class Test*` grouping** is spec'd as pure namespacing (spec/01 §7) but `_collect.collect` only
  looks for module-level `async def test_*` functions — a `Test*` class's methods are silently
  collected as zero tests, with no error and no skip entry. Flattened to free functions here.
- **`@velox.xfail`** is recorded on a function's marks but nothing reads it at run time (`_run.py`'s
  `Outcome` enum has four members: `PASSED`/`FAILED`/`ERROR`/`TIMEOUT` — no `XFAILED`/`XPASSED`
  yet), so a decorated test that "fails as expected" is reported plain `FAILED`. `skip` *is* wired
  end to end and is used in `test_users.py` where the reference stack would otherwise reach for
  `xfail`.
- **`-m`/tag-based selection** and most of the CLI surface documented in spec/02 §1
  (`-k`, `-v`/`-q`, `--serial`, `-x`, `--durations`, `--collect-only`, `path.py::test_name` id
  addressing) aren't wired into `cli.py` yet — `@velox.tag("slow")` on `test_bulk_signup` still
  records the tag, it just can't be selected against today.

None of these block a green run — `velox` with no arguments passes end to end (see "Expected
output" above) — they're just not what they'll eventually be.

## What is not here

No `conftest.py` — `tests/fixtures.py` is an ordinary module and the imports are explicit. No
`@pytest.mark.asyncio`. No `pytest-asyncio` event-loop-scope configuration. No `autouse`. No
`request` object beyond the read-only `velox.test_info`. No `monkeypatch` — see example 02.
