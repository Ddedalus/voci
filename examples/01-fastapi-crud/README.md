# 01 — FastAPI + async SQLAlchemy

The reference stack. A small CRUD API tested end to end through `httpx.AsyncClient` against a real
database, with a transaction-per-test that is always rolled back.

```
app/
  settings.py     frozen dataclass, injectable, reads env through a parameter
  models.py       User, Order
  db.py           get_session — the seam tests substitute
  main.py         routes + the module-level `app = FastAPI(...)`, written for production only
tests/
  database.py     the SQLAlchemy plumbing, kept out of the way of the fixtures
  fixtures.py     engine, session, api_client, alice, payment_sandbox
  test_users.py   the baseline shape: fixtures, skip/skipif, tags, parametrize
  test_orders.py  per-test override via a sibling fixture, raises/approx, built-ins, exclusive
```

## Setup

```bash
uv sync
```

The database is SQLite, so there is no server to start; `tests/database.py::url_for` is the one
line to change for Postgres. `pyproject.toml` sets `extend-immutable-calls = ["velox.Depends"]`
under `[tool.ruff.lint.flake8-bugbear]`, without which ruff's B008 fires on every `Depends(...)`
default.

## Commands

```bash
velox                     # everything (25 tests), up to 16 at once ([tool.velox] concurrency)
velox tests/test_users.py # one file
velox --concurrency 1     # exactly serial — the first debugging step
velox --timeout 5         # per-test setup+call budget; reported as TIMEOUT, not FAILED
velox -s                  # live, id-prefixed stdout/stderr instead of captured-on-failure
```

## Expected output

```
$ velox
config: /path/to/examples/01-fastapi-crud/pyproject.toml
PASS  tests/test_users.py                10 tests   Σ 3.66s
PASS  tests/test_orders.py               13 tests   Σ 7.92s
tests/test_users.py::test_list_users_is_paginated SKIPPED (pagination is not implemented yet (GET /users has no route -- 405, not 200))
tests/test_users.py::test_response_carries_request_id SKIPPED (middleware is behind a feature flag)
25 tests: 23 passed, 0 failed, 0 errored, 2 skipped, 0 collection error(s)

25 tests · 0 failed · 1.66s wall (7.0x concurrency)
```

`Σ 11.58s` is the serial cost of the 25 dispatched tests; `1.66s wall` is what you actually waited.

## What to look at

- **`tests/fixtures.py::engine`** — one engine for the whole run, shared by every concurrent test:
  the expensive thing gets built once, not once per test.
- **`tests/fixtures.py::session`** — the transaction that is always rolled back. Every test sees the
  real schema and none of its neighbours' writes.
- **`tests/fixtures.py::api_client`** — the tests run against `app/main.py`'s module-level `app`,
  unchanged. `dependency_overrides` and `state` are layered per test, so concurrent tests writing
  the same singleton is a solved problem rather than yours.
- **`test_orders.py::premium_client`** — a sibling fixture that rebuilds `api_client` with one
  dependency swapped, for one test, without touching the shared graph everyone else uses.
- **`tests/fixtures.py::payment_sandbox`** — `exclusive="payments-sandbox"` declared on the
  resource; every test that transitively depends on it inherits the token automatically.
- **`test_users.py::test_create_user`** — one `@velox.parametrize`d test in place of three
  near-identical functions; each case gets its own id (`test_create_user[bob]`,
  `test_create_user[tag-in-local-part]`, `test_create_user[subdomain]`) and runs concurrently with
  the rest of the suite.
