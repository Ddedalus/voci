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
  test_users.py   the baseline shape: parametrize, marks, class grouping
  test_orders.py  per-node override via a sibling fixture, raises/approx, built-ins, exclusive
```

## Setup

```bash
uv venv
uv pip install -r requirements.txt -e ../..
```

## Commands

```bash
velox                                     # everything, 16 tests in flight
velox -v                                  # one line per test, plus token contention
velox -q                                  # one character per file

velox tests/test_users.py                 # one file
velox 'tests/test_users.py::test_health'  # one test
velox 'tests/test_users.py::test_create_user[bob@example.com]'   # one parametrization
velox -k "order and not limit"            # keyword expression over the id
velox -m "not slow"                       # tag expression over @velox.tag

velox --serial                            # concurrency=1 — the first debugging step
velox --concurrency 64                    # if the database can take it
velox -x                                  # stop dispatching on the first failure
velox --durations 5                       # what to tune --concurrency against
velox --collect-only                      # ids in logical order, exit 0
```

Ids are byte-identical to pytest's addressing syntax, so anything already pinned in CI transfers
unchanged.

## Expected output

```
velox 0.1.0 · python 3.13.2 · uvloop · concurrency 16 · seed 0 · assert=rewrite

PASS  tests/test_users.py                  23 tests   0.68s
PASS  tests/test_orders.py                 16 tests   1.12s

39 tests · 37 passed · 1 xfailed · 1 skipped · 1.4s wall (Σ 8.9s, 6.4× concurrency)
```

The last line is the one to watch. `Σ 8.9s` is the serial cost of the same work; `1.4s wall` is what
you waited. When that ratio collapses, `--durations` and the exclusive-token summary under `-v` will
say why.

A failing run:

```
PASS  tests/test_users.py                  23 tests   0.71s
FAIL  tests/test_orders.py                 16 tests   1.19s   (1 failed)

──────────────────────── tests/test_orders.py::test_order_limit_is_enforced ────────────────────────

    limited = await client.post(url, json={"total_cents": 100})

>   assert limited.status_code == 429
E   assert 201 == 429
E    +  where 201 = <Response [201 Created]>.status_code

  tests/test_orders.py:88 in test_order_limit_is_enforced

short test summary
FAILED tests/test_orders.py::test_order_limit_is_enforced - assert 201 == 429

39 tests · 1 failed · 37 passed · 1 xfailed · 1.5s wall (Σ 9.1s, 6.1× concurrency)
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

## What is not here

No `conftest.py` — `tests/fixtures.py` is an ordinary module and the imports are explicit. No
`@pytest.mark.asyncio`. No `pytest-asyncio` event-loop-scope configuration. No `autouse`. No
`request` object beyond the read-only `velox.test_info`. No `monkeypatch` — see example 02.
