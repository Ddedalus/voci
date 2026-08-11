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
  test_users.py   the baseline shape: fixtures, skip/skipif, marks
  test_orders.py  per-node override via a sibling fixture, raises/approx, built-ins, exclusive
```

Cases that would otherwise be one `@velox.parametrize`d test (`test_create_user_bob` and its
neighbours in `test_users.py`) are written out as separate functions instead.

## Setup

```bash
uv venv
uv pip install -r requirements.txt -e ../..
```

`pyproject.toml` sets `extend-immutable-calls = ["velox.Depends"]` under
`[tool.ruff.lint.flake8-bugbear]`, without which ruff's B008 fires on every `Depends(...)` default.

## Commands

```bash
velox                     # everything (22 tests), up to 16 at once ([tool.velox] concurrency)
velox tests/test_users.py # one file
velox --concurrency 1     # exactly serial — the first debugging step
velox --timeout 5         # per-test setup+call budget; reported as TIMEOUT, not FAILED
velox -s                  # live, id-prefixed stdout/stderr instead of captured-on-failure
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

`Σ 11.13s` is the serial cost of the 22 dispatched tests; `1.14s wall` is what you actually waited.

## What to look at

- **`tests/fixtures.py::engine`** — one engine for the whole run, shared by every concurrent test:
  the expensive thing gets built once, not once per test.
- **`tests/fixtures.py::session`** — the transaction that is always rolled back. Every test sees the
  real schema and none of its neighbours' writes.
- **`tests/fixtures.py::api_client`** — the tests run against `app/main.py`'s module-level `app`,
  unchanged. `velox.fastapi.client` layers `dependency_overrides`/`state` through a `ContextVar` so
  concurrent tests each read and write their own layer of the same singleton.
- **`test_orders.py::premium_client`** — a sibling fixture that rebuilds `api_client` with one
  dependency swapped, for one test, without touching the shared graph everyone else uses.
- **`tests/fixtures.py::payment_sandbox`** — `exclusive="payments-sandbox"` declared on the
  resource; every test that transitively depends on it inherits the token automatically.
