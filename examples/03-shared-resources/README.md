# 03 — Shared resources, exclusion, and the safety net

Standalone: stdlib only. A ledger over `sqlite3`, a migration chain, a webhook receiver on a fixed
port, and a process-global feature flag registry — one of each kind of thing concurrency breaks.

```
ledger/
  store.py        blocking sqlite3 — every method blocks its caller's thread
  migrations.py   an ordered schema chain
  service.py      async wrapper via to_thread, plus balance_blocking() — the trap
  flags.py        a module-level dict read at call time
  receiver.py     a TCP server on port 8099
tests/
  fixtures.py         three resources, three different answers
  test_ledger.py       no tokens — every test owns its own account namespace, fully concurrent
  test_migrations.py   exclusive=True
  test_webhooks.py     exclusive="port-8099" — one test, four scenarios run in sequence
  test_safety.py       @voci.solo, and a blocking call that stalls the loop
```

## Setup

```bash
uv venv && uv pip install -e ../..
```

## Commands

```bash
voci                    # everything (24 tests, 1 skipped)
voci --concurrency 1    # exactly serial
voci --timeout 5        # per-test setup+call budget
```

## Expected output

```
$ voci
assertions: rewrite, cache /home/you/.cache/voci/rewrite
config: /path/to/examples/03-shared-resources/pyproject.toml
PASS  tests/test_migrations.py                    7 tests  Σ 0.75s
PASS  tests/test_webhooks.py                      1 test   Σ 0.07s
PASS  tests/test_ledger.py                       11 tests  Σ 9.25s
PASS  tests/test_safety.py                        5 tests  Σ 1.36s   (1 skipped)

24 tests · 23 passed · 1 skipped · 2.09s wall (5.5x concurrency)
```

## What to look at

- **`tests/fixtures.py::account`** derives a namespace from `voci.test_info.id`, so all eleven
  tests in `test_ledger.py` share one database with no token at all — isolating by data instead of
  by exclusion.
- **`tests/fixtures.py::migration_db`** — `exclusive=True`: the token is the fixture's own name,
  and every test whose dependency graph transitively reaches it inherits that token.
- **`tests/fixtures.py::receiver`** — `exclusive="port-8099"` names the port itself rather than the
  fixture, so any other fixture binding the same port would carry the same token.
  `test_webhooks.py::test_receiver_scenarios` runs its four scenarios in sequence inside one test,
  since two tests binding port 8099 at once is a real `OSError`, not a scheduling nuance.
  The same test also holds two tokens at once (`port-8099` and `migration_db`) for its last
  scenario.
- **`tests/fixtures.py::feature_flags`** pairs with `@voci.solo` in `test_safety.py` for a
  module-level dict with no per-task view. `test_strict_transfers_rejects_overdraft` is marked
  `skip` because it flips a flag that `test_ledger.py::test_transfer_is_permitted_to_overdraw_by_default`
  depends on staying off; `test_audit_flag_is_restored_afterwards` flips one nothing else reads, and
  runs.
- **`ledger/service.py`'s `balance_blocking`** and `test_safety.py::test_blocking_call_stalls_the_loop`
  — a synchronous method called directly from a coroutine runs on the event loop thread, freezing
  every other in-flight test for its duration. `LedgerService.balance` wraps the same call in
  `asyncio.to_thread` instead.
