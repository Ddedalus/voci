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
  test_ledger.py      no tokens — 32 in flight
  test_migrations.py  exclusive=True
  test_webhooks.py    exclusive="port-8099", and one test holding two tokens
  test_safety.py      @velox.solo, the watchdog, timeouts
```

## Setup

```bash
uv venv && uv pip install -e ../..
```

## Commands

```bash
velox                                              # 32 in flight
velox -v                                           # per-test lines + token contention summary
velox -m "not slow"

velox -k watchdog-demo --watchdog-threshold 0.05   # make the watchdog fire
velox --watchdog fail                              # a stall fails the blocking test
velox --serial                                     # tokens and solo become no-ops

velox --durations 10                               # tune --concurrency against this
velox --collect-only                               # logical order, exit 0
```

## Expected output

```
velox 0.1.0 · python 3.13.2 · uvloop · concurrency 32 · seed 0 · assert=rewrite

PASS  tests/test_ledger.py                 13 tests   0.61s
PASS  tests/test_migrations.py              8 tests   0.44s
PASS  tests/test_webhooks.py                4 tests   0.18s
PASS  tests/test_safety.py                  5 tests   0.37s

  exclusive tokens
      migration_db        8 tests   0.44s held
      port-8099           4 tests   0.18s held

  2 tests ran solo · suite drained for 0.11s (9% of wall clock)

  ⚠ 1 loop stall
      tests/test_safety.py::test_blocking_call_stalls_the_loop  blocked 0.31s
        ledger/store.py:34 in balance

30 tests · 30 passed · 1.2s wall (Σ 6.8s, 5.7× concurrency)
```

## The three answers

### No token — isolate by data (`test_ledger.py`, 13 tests)

Thirteen tests share one database and one connection path, and none of them conflict, because the
`account` fixture derives an account namespace from `velox.test_info.id`. Parametrizations get
distinct ids, so they get distinct namespaces, so all four cases of `test_balance_arithmetic` run
at the same instant without knowing about each other.

**This is the answer most of the time, and it is worth trying before reaching for a token.** A test
that owns its data needs no scheduler cooperation at all.

### `exclusive=True` — one resource, one test at a time (`test_migrations.py`, 8 tests)

The token is the fixture's own name. Every test whose dependency graph transitively reaches
`migration_db` inherits `migration_db` in its exclusive set, and the scheduler never admits two of
them together.

Crucially they are serialised **only against each other**. While the migration tests take turns,
the thirteen ledger tests are running at full width. That is the difference between an exclusive
token and `@velox.solo`.

### `exclusive="token"` — a named resource (`test_webhooks.py`, 4 tests)

Port 8099 is not owned by the `receiver` fixture; the fixture is one way to reach it. A string
token names the resource itself, so a second fixture that also bound 8099 would carry the same
token and be kept apart from the first automatically.

### Two tokens at once

`test_migration_notifies_the_receiver` depends on both `receiver` and `migration_db`, so its
exclusive set is `{port-8099, migration_db}`.

This is normally where a runner needs lock ordering, acquisition timeouts, or deadlock detection.
None of that is here. A test's exclusive set is known *statically at collection*, and the scheduler
acquires the **entire set atomically or not at all**. No test ever holds one token while waiting for
another, so there is no hold-and-wait, so deadlock is impossible by construction rather than by
discipline.

That guarantee is downstream of explicit dependency injection, and it is the concrete reason velox
has no `getfixturevalue`: a dependency discovered at run time is a footprint that cannot be known
before dispatch, and the property evaporates.

## `@velox.solo` — when there is no per-task view at all

`ledger/flags.py` is a module-level dict read at call time. There is no ContextVar underneath it and
no way to give two concurrent tests different answers, so a test that flips a flag has to run alone.

Note what is *not* involved: no mocking library, no patching, no `monkeypatch`. Solo is not the
mocking tier — it is the answer to process-global state in general, and mocking is one instance of
it. (`test_ledger.py::test_transfer_is_permitted_to_overdraw_by_default` asserts the flag's default
behaviour, which is exactly the test that must not be in flight while the flag is flipped.)

The `feature_flags` fixture snapshots and restores the registry. That makes the mutation
*reversible*, not *invisible* — the decorator is still required.

## The watchdog

`LedgerService.balance_blocking` is the trap: a perfectly ordinary-looking synchronous accessor.
Call it from a coroutine and it runs on the event loop thread. Every other in-flight test freezes,
no timeout fires, no exception is raised, and the only symptom is that the suite got slower.

velox notices from outside the loop — a daemon thread watching a 100 ms heartbeat — and when the
timestamp goes stale it captures every task stack plus a `faulthandler` dump of every *thread*
stack, because the blocking frame lives in a thread stack, not a task stack.

```
velox -k watchdog-demo --watchdog-threshold 0.05
```

```
⚠ Event loop blocked for 0.31s during
  tests/test_safety.py::test_blocking_call_stalls_the_loop
    blocking frame:  ledger/store.py:34 in balance
    5 other tests were stalled: test_balance_sums_entries, test_entries_are_ordered, ...
    → wrap blocking calls in `await asyncio.to_thread(...)`
```

That diagnosis is worth more than the test that triggered it. The same call in a request handler
blocks the production server in exactly the same way, and a serial runner has no way to notice —
there is nothing else running to be starved.

## What this costs you

Being honest about the bill, since that is what a reader evaluating adoption needs:

- **Three fixtures had to declare a constraint.** One keyword argument each, on the resource, where
  the fact lives. No test annotates itself and no test can forget to.
- **Two tests serialise the whole suite.** 9% of wall clock here, and the summary says so on every
  run. If it grows, you find out from the summary rather than from a stopwatch.
- **Order-dependent tests will fail**, and that is the real migration cost. Anything that passed
  only because pytest ran it after something else breaks here. The documented workflow is: migrate,
  run `--concurrency=1` (should be green), then raise concurrency and triage. The two-step is the
  single most useful thing to know before starting.
- **Blocking calls become everyone's problem** rather than just their own test's. The watchdog is
  the compensation: it names them instead of letting them hide in the wall clock.
