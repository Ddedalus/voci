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
  test_ledger.py      no tokens — every test owns its own account namespace, fully concurrent
  test_migrations.py  exclusive=True, declared (not yet enforced — see "Known gaps")
  test_webhooks.py    exclusive="port-8099" — one test, four scenarios (see "Known gaps")
  test_safety.py      @velox.solo, real timeouts, and what the (not yet built) watchdog would say
```

## Setup

```bash
uv venv && uv pip install -e ../..
```

## Commands

This is the current, working CLI surface, not the eventual one — see
[`examples/01-fastapi-crud`'s README](../01-fastapi-crud/README.md#commands) for the full list of
what's still missing. Nothing here uses `-k`/`-m`/`--watchdog*`/`--serial`/`--durations`/
`--collect-only` for the same reason: none of them exist yet either.

```bash
velox                    # everything (23 tests, 1 skipped)
velox --concurrency 1    # exactly serial
velox --concurrency 64
velox --timeout 5
```

## Expected output

```
$ velox
assertions: rewrite, cache /home/you/.cache/velox/rewrite
config: /path/to/examples/03-shared-resources/pyproject.toml
PASS  tests/test_migrations.py            7 tests   Σ 1.62s
PASS  tests/test_webhooks.py              1 tests   Σ 0.57s
PASS  tests/test_safety.py                4 tests   Σ 0.90s
PASS  tests/test_ledger.py               11 tests   Σ 7.58s
tests/test_safety.py::test_strict_transfers_rejects_overdraft SKIPPED (would race ...)
23 tests: 23 passed, 0 failed, 0 errored, 1 skipped, 0 collection error(s)

23 tests · 0 failed · 1.72s wall (Σ 10.66s, 6.2x concurrency)
```

No exclusive-token summary, no solo-cost line, no watchdog warning — see "Known gaps" below for
why: none of those three reporting sections exist yet, because the scheduling machinery under them
doesn't either. The one `SKIPPED` line is the test that would have needed real `@velox.solo`
enforcement to run safely alongside its neighbour.

## The three answers

### No token — isolate by data (`test_ledger.py`, 11 tests)

Eleven tests share one database and one connection path, and none of them conflict, because the
`account` fixture derives an account namespace from `velox.test_info.id`. Distinct test ids get
distinct namespaces, so all four `test_balance_arithmetic_*` cases (spelled out by hand — see
"Known gaps" — rather than one `@velox.parametrize`d test) run at the same instant without knowing
about each other.

**This is the answer most of the time, and it is worth trying before reaching for a token.** A test
that owns its data needs no scheduler cooperation at all — and, concretely in this codebase today,
no scheduler cooperation is what actually exists (see below), so this file is also the one that
needed nothing changed to run correctly.

### `exclusive=True` — declared, not yet enforced (`test_migrations.py`, 7 tests)

The token is the fixture's own name: every test whose dependency graph transitively reaches
`migration_db` inherits `migration_db` in its exclusive set. Once a scheduler consults that set
(it doesn't yet — `_run.py`'s own module docstring lists "exclusive-resource admission" as still
deferred; see "Known gaps"), it would serialize them **only against each other**, while the eleven
ledger tests keep running at full width — the difference between an exclusive token and
`@velox.solo`.

Nothing here is unsafe in the meantime: `migration_db` hands each test its own file via
`tmp_path` (the fixture's own docstring says so), so there's no real shared state for concurrent
migration tests to corrupt today, only the token's *intent* to demonstrate.

### `exclusive="token"` — a named resource, and a real conflict (`test_webhooks.py`)

Port 8099 is not owned by the `receiver` fixture; the fixture is one way to reach it. A string
token names the resource itself, so a second fixture that also bound 8099 would carry the same
token and be kept apart from the first automatically — once that scheduling exists.

Unlike the migration database, this one is a genuine OS-level singleton, and there is no scheduler
yet to keep two `receiver`-using tests apart: `ledger/receiver.py`'s own docstring calls this case
out ("two of these on the same port is an `OSError`, not a flaky test") and running the four
original scenarios as four separate tests reproduced exactly that, deterministically, every time.
They're one test now — `test_receiver_scenarios`, all four scenarios run in sequence against one
`receiver` instance — see "Known gaps" for the full reasoning and what to restore once admission is
real.

### Two tokens at once

The two-token scenario inside `test_receiver_scenarios` (`port-8099` and `migration_db` together)
is the case that would normally make a runner need lock ordering, acquisition timeouts, or deadlock
detection. The design reason none of that would be needed, once the scheduler exists: a test's
exclusive set is known *statically at collection*, and admission would acquire the **entire set
atomically or not at all**. No test would ever hold one token while waiting for another, so there
would be no hold-and-wait, so deadlock would be impossible by construction rather than by
discipline.

That guarantee is downstream of explicit dependency injection, and it is the concrete reason velox
has no `getfixturevalue`: a dependency discovered at run time is a footprint that cannot be known
before dispatch, and the property would evaporate.

## `@velox.solo` — when there is no per-task view at all

`ledger/flags.py` is a module-level dict read at call time. There is no ContextVar underneath it and
no way to give two concurrent tests different answers, so a test that flips a flag needs to run
alone.

Note what is *not* involved: no mocking library, no patching, no `monkeypatch`. Solo is not the
mocking tier — it is the answer to process-global state in general, and mocking (see
[example 02](../02-async-library/)) is one instance of it.

`@velox.solo` isn't enforced yet, the same gap as `exclusive=` above (see "Known gaps"), and the
two solo-marked tests here split the same way example 02's did: `test_strict_transfers_rejects_
overdraft` flips `strict_transfers`, which `test_ledger.py::test_transfer_is_permitted_to_
overdraw_by_default` depends on staying off — a real, verified collision, so it's skipped rather
than run unguarded. `test_audit_flag_is_restored_afterwards` flips a flag nothing else in this
suite reads, so it stays live: the mark is honest documentation of intent either way, but only one
of the two currently has a live neighbour to actually corrupt.

The `feature_flags` fixture snapshots and restores the registry either way. That makes the mutation
*reversible*, not *invisible* — the decorator is still the thing that would keep it from being
visible to everyone else while it's flipped, once it's real.

## The watchdog (roadmap)

`LedgerService.balance_blocking` is the trap: a perfectly ordinary-looking synchronous accessor.
Call it from a coroutine and it runs on the event loop thread. Every other in-flight test freezes,
no timeout fires, no exception is raised, and the only symptom is that the suite got slower.

The plan is for velox to notice this from outside the loop — a daemon thread watching a heartbeat,
capturing every task stack plus a `faulthandler` dump of every *thread* stack once the heartbeat
goes stale, since the blocking frame lives in a thread stack, not a task stack — and report
something like:

```
⚠ Event loop blocked for 0.31s during
  tests/test_safety.py::test_blocking_call_stalls_the_loop
    blocking frame:  ledger/store.py:34 in balance
    5 other tests were stalled: test_balance_sums_entries, test_entries_are_ordered, ...
    → wrap blocking calls in `await asyncio.to_thread(...)`
```

None of that exists yet — no watchdog thread, no `--watchdog-threshold` flag, and the
`watchdog_threshold` key this example's `pyproject.toml` would otherwise set is rejected outright
by the config loader rather than silently ignored (`docs/M1-PLAN.md`: "no consumer before M2").
`test_blocking_call_stalls_the_loop` still runs and still passes — the underlying bug's *symptom*
(a real, if brief, stall) happens whether or not anything is watching for it — but nothing
diagnoses it today. That diagnosis, once it exists, is worth more than the test that triggers it:
the same call in a request handler blocks the production server in exactly the same way, and a
serial runner has no way to notice — there is nothing else running to be starved.

## Known gaps (tracked, not bugs in this suite)

Dogfooding this example surfaced the same declared-but-not-yet-acted-on pattern as
[examples 01](../01-fastapi-crud/README.md#known-gaps-tracked-not-bugs-in-this-suite) and
[02](../02-async-library/README.md#known-gaps-tracked-not-bugs-in-this-suite), plus the scheduling
machinery this example's whole subject depends on. All tracked in
[`docs/M1-PLAN.md`](../../docs/M1-PLAN.md):

- **`exclusive=`/`@velox.solo` aren't enforced.** `_run.py`'s own module docstring says so
  directly: "exclusive-resource admission, the solo write-lock tier, aging... still deliberately
  not built this slice." A test carrying either mark runs exactly like one that doesn't — no
  serialization, no suite-wide lock. Safe where nothing actually conflicts (`test_migrations.py`,
  `test_audit_flag_is_restored_afterwards`); a real, verified problem where it does
  (`test_webhooks.py`'s fixed port, `test_strict_transfers_rejects_overdraft`'s shared flag) —
  both handled here by not running the conflicting shape concurrently, rather than pretending the
  mark already protects it.
- **The loop-starvation watchdog doesn't exist**, and neither does the config key
  (`watchdog_threshold`) or CLI flag (`--watchdog-threshold`) that would configure it.
- **`@velox.parametrize`** — the same gap examples 01 and 02 found: `test_balance_arithmetic_*`/
  `test_migrates_to_version_*` are parametrize cases written out by hand (`_collect.collect`
  doesn't expand `@velox.parametrize` into records yet).
- **A test returning a value, or leaving a coroutine un-awaited, isn't caught.** I8 ("silent
  passes are bugs") names this failure shape explicitly, but nothing in `_run.py` inspects a
  test's return value and no warnings filter escalates an un-awaited-coroutine `RuntimeWarning` to
  a failure yet. `test_awaiting_actually_runs_the_coroutine` is a plain sanity check, not a
  demonstration of an enforced rule — see its own docstring.

None of these block a green run — `velox` with no arguments passes end to end (see "Expected
output" above).

## What this costs you

Being honest about the bill, since that is what a reader evaluating adoption needs — read this
section as "once the scheduling above is real," not as this example's current behavior:

- **Three fixtures had to declare a constraint.** One keyword argument each, on the resource, where
  the fact lives. No test annotates itself and no test can forget to.
- **A couple of tests would serialise the whole suite.** The summary would say so on every run. If
  it grows, you'd find out from the summary rather than from a stopwatch.
- **Order-dependent tests will fail**, and that is the real migration cost. Anything that passed
  only because pytest ran it after something else breaks here. The documented workflow is: migrate,
  run `--concurrency 1` (should be green), then raise concurrency and triage. The two-step is the
  single most useful thing to know before starting — and the one part of this list that's already
  true today, since `--concurrency` itself is real (spec/05).
- **Blocking calls become everyone's problem** rather than just their own test's, watchdog or not
  — `test_blocking_call_stalls_the_loop` above stalls the whole suite for real today, it's just
  that nothing names it yet. The watchdog is the compensation: it would name them instead of
  letting them hide in the wall clock.
