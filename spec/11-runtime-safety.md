# 11 — Runtime Safety: watchdog, signals, warnings, unraisables, coverage

*These are R§8's "concerns not in the original list". They are grouped here because they share a
property: each is a way a concurrent single-process runner can fail in a manner that is confusing
rather than merely wrong. The watchdog in particular is a **first-release feature, not a backlog
item** — and an adoption argument in its own right.*

---

## 1. Loop-starvation watchdog

**The problem.** One accidentally-blocking call — `requests`, a sync DB driver, `bcrypt`, a
`time.sleep` deep in a library — freezes the entire concurrent suite, and it is undetectable from
inside the loop, because the loop is what's frozen.

**The mechanism.**

- A daemon **thread** (outside the loop) and a loop heartbeat: a callback scheduled every 100 ms
  writes a monotonic timestamp; the thread checks the age of that timestamp.
- Age > `--watchdog-threshold` (default 1.0 s) ⇒ **stall detected**. The thread:
  1. captures all task stacks (`asyncio.all_tasks` + `Task.get_stack()`), plus a `faulthandler`
     dump of all *thread* stacks — the blocking frame is in a thread stack, not a task stack;
  2. names the in-flight tests (from the scheduler's in-flight map);
  3. under `--watchdog=warn` (default), records a stall event reported at the end; under
     `--watchdog=fail`, marks the test that was running on the loop thread as `failed`.

**The output is the feature:**

```
⚠ Event loop blocked for 4.2s during tests/auth/test_login.py::test_password_hash
    blocking frame:  bcrypt/__init__.py:112 in hashpw
    3 other tests were stalled: test_refresh, test_logout, test_session_expiry
    → wrap blocking calls in `await asyncio.to_thread(...)`
```

"test_login blocked the event loop for 4.2 s — here's the stack" diagnoses a **production** bug, not
just a test bug. That is worth saying in the README.

**Crash forensics.** The same thread writes the current in-flight set to
`.velox/in-flight.json` on each check. This is the concurrent replacement for `PYTEST_CURRENT_TEST`
(which is meaningless with N tests running): after a hard crash or an OOM kill, that file says what
was running.

## 2. Signals

**Ctrl-C choreography** (R§8.3) — leaked containers and schemas after a Ctrl-C are a terrible first
impression, so this is specified precisely:

1. `loop.add_signal_handler(SIGINT, ...)` — not the default KeyboardInterrupt path, which lands at
   an arbitrary bytecode boundary.
2. Cancel the top-level TaskGroup. Stop dispatching immediately.
3. In-flight tests report `interrupted`.
4. **Teardowns run under `asyncio.shield` with a hard timeout** (`teardown_grace`, default 10 s),
   session scopes included, in refcount order.
5. The reporter flushes completed blocks, prints what ran, prints what leaked if a teardown
   exceeded its grace.
6. Exit `2`.
7. **A second Ctrl-C during any of this: `os._exit(2)`** immediately. A user pressing Ctrl-C twice
   has decided; do not make them press it a third time.

`SIGTERM` follows the same path (CI cancellation). `SIGQUIT` (where available) triggers a
faulthandler dump without terminating — a free "what is it doing right now" for a hung CI job.

## 3. Warnings

**The problem.** Warnings filters are process-global; `catch_warnings` saves and restores a global
list, and concurrent restores clobber each other. Python 3.14's context-aware warnings fix this
properly, but 3.13 (our floor) cannot rely on it (R§8.2).

**The design:** stop asking CPython to decide, and decide in the shim instead.

- The process-wide filter is set to `always`, so every warning reaches a `showwarning` shim
  undropped, attributed to a test via a ContextVar ([09](09-capture-and-logging.md)).
- The shim evaluates the filter stack itself, reading the *raising test's* filters off that
  ContextVar. Per-test `filterwarnings` marks are therefore correct at any concurrency, including
  suppression and escalation — no serial-mode restriction, and nothing to lift when 3.14 becomes
  the floor.
- Three tiers, lowest first: config `filterwarnings = [...]`, `-W`, then the test's own marks. The
  last filter to match a warning decides it.
- `error` raises the warning out of the `warnings.warn(...)` that produced it, failing the phase
  that reached that call.
- Warnings that survive filtering are aggregated by warning and location and reported at the end of
  the run, alongside a `warnings` field in `--report-json`.
- What remains process-global is `catch_warnings()` in user code: a test that enters one changes
  what its concurrent siblings see, for as long as it is open.

## 4. Unraisables and orphan task exceptions

- `sys.unraisablehook` — exceptions in `__del__`, GC callbacks, and similar. Attributed via the
  capture ContextVar where possible; otherwise reported in a session-level section. An unraisable
  attributable to a test fails it (configurable, `unraisable = "fail" | "warn"`).
- `loop.set_exception_handler` — "Task exception was never retrieved", transport errors. Same
  attribution path.
- Tasks the user spawned outside the test's TaskGroup and never awaited are covered by the leaked-task
  check in [05](05-execution-model.md) §2; anything that escapes even that lands here.

Together these close the "something went wrong and nobody was told" gap that concurrency widens.

## 5. Coverage

**The one hard plugin-compatibility requirement** (R§1, R§8.9). `coverage.py` must work, and it
does: it is `sys.monitoring`-based on modern CPython, and single-process concurrency is fine for it
— unlike xdist, which needs `--cov-append` plus a combine step.

Requirements:
- `coverage run -m velox` works with no velox-side special-casing. **Verified in CI from the first
  prototype**, not at the end.
- `--isolated` subprocesses must inherit coverage (`COVERAGE_PROCESS_START` + the subprocess hook) —
  this is the one place velox has to do something, and it is on the roadmap with the isolated tier.
- Document that no `--cov-append`/`combine` dance is needed. That is another head-to-head win worth
  stating.

## 6. Free-threading horizon

Every ContextVar-based design here — capture sinks, assertion hooks, patch overrides, log routing —
is also the correct answer under free-threaded CPython (3.13t+). The binding rule is I1: **no new
module-global mutable state anywhere** (R§8.6). No work is planned for free-threading in v1; the
point is not to foreclose it.

## 7. MVP

Watchdog (thread, heartbeat, stall report, in-flight file); full Ctrl-C/SIGTERM choreography with
shielded teardown and the double-Ctrl-C hard exit; global warning collection with attribution and
the loud "filters not applied" notice; `sys.unraisablehook` and the loop exception handler; a
CI job proving `coverage run -m velox` works.

## 8. Roadmap

- `--watchdog=fail` refinement: attributing a stall to the *right* test when several are in flight
  requires knowing which task the loop was executing at stall time — recoverable from the heartbeat
  callback's last-run task, but needs care.
- Coverage inheritance into `--isolated` subprocesses.
- SIGQUIT dump on all platforms that have it; a `--dump-on-stall` flag that writes the full stack
  dump to a file.
- Per-test warning filters becoming real once 3.14 is the floor.
- A `velox doctor` command: checks rewrite-cache writability, uvloop availability, loop-blocking
  library imports (`requests`, `psycopg2`, sync `redis`) present in the test environment, and prints
  the concurrency-readiness verdict.

## 9. Open questions

- **Q22** — Default `--watchdog=warn` or `fail`? `warn` is proposed for v0.1 because a
  false-positive stall (a legitimate CPU-bound test, a GC pause on a big fixture) failing a
  previously-green suite is a bad first impression. Once the attribution is precise, `fail` is the
  better default and should probably flip.
- **Q23** — Watchdog threshold of 1.0 s: too tight for suites doing legitimate CPU work in sync
  tests (which run in the executor and so *don't* block the loop — but a big `json.loads` in an
  async test does). Proposed: 1.0 s, with the stall report explaining how to raise it.
- **Q24** — Should velox refuse to start if a known-blocking library is imported in the test
  environment? No — far too blunt. That belongs in `velox doctor` as advice.
