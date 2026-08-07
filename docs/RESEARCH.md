# velox — pytest research synthesis & design recommendations

*2026-08-06. Sources: three deep dives into the pytest source cloned at `pytest/`
(assertion rewriting; collection + fixtures; execution/capture/reporting/parallelism prior art).
All measurements were taken against this checkout (pytest 9.2.0.dev, CPython 3.13) on a
synthetic suite of 200 files × 25 tests = 5000 tests unless stated otherwise.*

Decisions already made: single process + one long-lived event loop with tests as concurrent
asyncio tasks; plain-assert introspection if cheap; velox-native explicit-DI API with codegen
migration from pytest (no runtime shims); deterministic schedule (dispatch order a pure
function of test set + seed).

---

## 1. Verdicts on the founding premises

| Premise | Verdict |
|---|---|
| Implicit name-based fixture resolution is slow/complex | **Validated.** ~600 LOC / 26% of collection time, all deletable under explicit DI. But resolution is not the hard part of fixtures — lifecycle is, and it survives (§4). |
| Collection is slow; skip it jest-style | **Validated, wrong culprit.** Only ~8% of collection cost is importing test modules; the rest is pytest's own node tree + fixture-closure bookkeeping. The fix is "no node tree, per-file laziness", not "avoid imports" (§3). |
| Assertion-rewrite magic may be expensive to lift | **Refuted — it's cheap.** Verbatim extraction of `rewrite.py` is a measured 30-line diff. Vendor it (§2). |
| Deterministic order vs concurrency is a tension | **Dissolved** by splitting logical order from physical order (§6). |
| Exclusive resources risk deadlock | **Dissolved** by explicit DI: resource sets are static, so all-or-nothing admission makes deadlock structurally impossible (§6). |
| Plugins are skippable | **Mostly.** One hard check: `coverage.py` must work (it does — it's `sys.monitoring`-based and single-process concurrency is fine for it, unlike xdist which needs `--cov-append` + combine). Everything else is out of scope by design (§8.9). |

The single biggest surprise: **the run phase is a bigger prize than collection.** Pytest spends
~600 µs of pure protocol overhead per test (three pluggy multicalls per test through 52 hooks,
report construction, capture suspend/resume) — ~3.4× the entire collection cost on the
benchmark suite. Real fixture work was only ~13% of it. Velox's hot path must have zero
hook dispatch and build failure representations lazily.

---

## 2. Assertion introspection — the priced question

### How pytest does it (mechanism in one paragraph)

A meta-path finder at `sys.meta_path[0]` intercepts imports of test files/conftests only
(name-based early bailout ~5–8 µs per non-test import). Matching modules get an AST pass:
each `assert` is replaced by code that hoists every subexpression into a fresh temp
(evaluated exactly once — side-effect-correct), then an `if not <cond>:` branch builds a
`%`-formatted explanation from `saferepr`s of the temps and raises. All expensive work lives
inside the failure branch. Line numbers survive via `ast.copy_location` fixups; rewritten
`.pyc`s are cached under a pytest-versioned tag with mtime+size invalidation. Comparison
diffs (`==` on sequences/dicts/sets/dataclasses/text) are a separate, decoupled ~600 LOC
explanation engine (`assertion/util.py` + `_compare_*.py` + streaming truncation).

### Measured costs

- **Passing asserts are effectively free**: +4 ns (`x == y`) to +37 ns (chained/boolop)
  per assert vs plain; scales with subexpression count, never with value size.
- **Cold import penalty 4.6×** (parse+rewrite+compile vs plain compile; the pure-Python AST
  pass dominates at ~164 µs/assert). **Warm pyc load is 154× faster than cold** — the cache
  is load-bearing, not an optimization. Bytecode grows 1.7–2×.
- **Extraction is a 30-line diff** (empirically performed during research): `rewrite.py`
  (1193 LOC) + vendored `saferepr` (155) + ~130 LOC shim ≈ 1480 LOC standalone, verified
  working including `await` inside asserts. With the explanation engine: ~2400 LOC total.
  The 5.8k lines of pytest tests covering this feature are the real asset — vendoring keeps
  them applicable; reimplementing forfeits them.

### Decision: vendor, with three fixes

1. **Convert `util._reprcompare` / `util._assertion_pass` / `util._config` to `ContextVar`s.**
   They are module globals save/restored per test item — the one genuine concurrency blocker
   in the whole subsystem. ~10 lines; asyncio propagates context into tasks automatically.
   The rewritten code itself is concurrency-safe (all temps are frame-locals).
2. Change the injected helper-module name (`rewrite.py:719` hardcodes
   `"_pytest.assertion.rewrite"` into generated pycs) and put a velox rewriter version in the
   pyc tag. Key the temp-pyc name on (pid, thread) not pid alone; replace the `_writing_pyc`
   bool guard with a lock.
3. **Layer PEP 657 as the universal floor**: pytest rewrites *only* test files — an assert in
   a helper module gives a bare `AssertionError`. A ~50-LOC fallback that renders the
   `co_positions()` caret span (3.11+) for any un-rewritten assertion closes pytest's own
   biggest fidelity gap at ~zero cost. (Alternatives ranked and rejected: safe partial
   re-eval via `executing`-style introspection gives ~80% fidelity with zero import cost —
   worth keeping in mind as a `--no-rewrite` mode for cold/read-only environments;
   naive re-evaluation on failure is unsound with side effects — pytest shipped exactly that
   as `--assert=reinterp` and deleted it in 3.0; explicit `expect()` ships as an option,
   never the only path.)

**Watch-out**: velox will be benchmarked cold in CI containers. Guarantee a writable cache
(consider `sys.pycache_prefix` into a velox-owned dir) or fall back to the no-rewrite mode
automatically when the cache is unwritable — never silently pay 4.6× per run.

Small stuff that's easy to miss: filter `@`-prefixed rewriter temps from any locals display;
include every codegen-affecting option in the pyc cache key (pytest's
`enable_assertion_pass_hook` is a documented footgun for not being in it).

---

## 3. Collection: what to keep, what to kill

### Where pytest's 0.87 s (5000 tests) actually goes

~40% node instantiation — `_genfunctions` creates **two** node objects per test (a throwaway
`FunctionDefinition` plus the real `Function`), each with mark unpacking, keyword dicts,
stashes, requests. ~26% fixture-closure resolution (`inspect.signature` per function +
ancestor-chain walks; 250k `iter_parents` calls). ~8% importing the test modules. Startup
adds 130–300 ms: 28 default plugins, three argv parses (plugins add options mid-flight),
entry-point autoload that is O(installed plugins) whether used or not.

### The load-bearing subset a "just run it" design still needs

- **Test IDs** `relpath::qualname[param]` — compute directly from file + `__qualname__` +
  param id; no parent-chain derivation. Keep pytest's exact addressing syntax (muscle memory,
  CI configs, `--deselect`).
- **Parametrize** — genuinely cheap in pytest (~24 µs/item; per-function fixture info is
  shared across callspecs). Keep, expand per-file inside the worker.
- **Marks/skip/xfail** — pytest already evaluates these lazily at runtime (`skipping.py`),
  not at collection. Same split, far less code. Keep `xfail(strict=)` semantics.
- **`-k`/`-m`** — both are pure predicates over a single item; no global item list needed.
  A small expression parser (pytest's is 353 LOC) is warranted.
- **Import mode: importlib-only, one position.** No `sys.path` mutation, no `__init__.py`
  requirement, path-derived unique module names. This single decision deletes `Package`,
  `ImportPathMismatchError`, namespace-package config, and pytest's worst legacy tax.

### Kill entirely

Node tree/parent pointers, per-path hook proxies (`FSHookProxy`), conftest directory
hierarchies (one root config module, explicit registration), `pytest_collection_modifyitems`,
`reorder_items` (exists only to serve a *sequential* scope cache — under concurrency it
concentrates contention), unittest/doctest collection, dynamic `getfixturevalue` (its
existence is what forces pytest to treat every fixture closure as incomplete).

### Build what pytest never did: a persistent collection cache

`.pytest_cache` does **not** cache collection — `--lf` re-collects everything and filters.
A per-file test-ID index keyed on (mtime, size) or content hash makes `--lf`, failure-first
scheduling, and instant test counts genuinely cheap. Real differentiator, small code.

Discovery itself = one `os.scandir` walk with a compiled name filter and a fixed ignore set.
Accept jest's trade: total test count is unknown until files load.

---

## 4. Fixtures: explicit DI replaces resolution, not lifecycle

Honest framing: explicit injection deletes the registry, conftest visibility rules,
proximity-override chains, autouse name walks, and `FixtureLookupError` machinery
(~600 of `fixtures.py`'s 2598 LOC, and the conceptual complexity users hate). It does **not**
delete the hard ~700 LOC, which any design needs:

- **Scope-keyed caching with param invalidation** — pytest caches `(value, cache_key, exc)`
  per fixturedef and tears down + re-runs on param mismatch. Exception caching is load-bearing:
  a broken session-scoped DB fixture must produce one comprehensible error, not 400.
- **Teardown inversion** — a fixture's finalizer is registered on *each of its dependencies*
  so dependents unwind before dependencies. Subtle; velox will reinvent it, so plan it.
- **Async-native lifecycle** — yield-fixtures become async generators driven by an
  `AsyncExitStack` per scope instance.

Two structural upgrades explicit DI enables:

1. **Static validation at import time.** The full dependency graph is the literal object
   graph of referenced callables — validate scope compatibility ("session depends on
   function" → error) once, before anything runs. Pytest can only discover this lazily.
2. **Concurrency-correct caching.** `FixtureDef.cached_result` is an unguarded
   read-modify-write that survives only because pytest is sequential. Velox: single-flight
   `Future` per (fixture, scope-instance, param-key) — first requester constructs, others
   await. **Teardown by refcount, not by pytest's `SetupState` stack** — the stack encodes
   "fixture lifetime = contiguous run of adjacent tests," which is false under concurrency.
   A scope instance tears down when its refcount hits zero (or at session end for session
   scope). Collect concurrent teardown errors into `ExceptionGroup`s.

Session scope note: xdist duplicates session fixtures per worker (N schemas, N engines for
`-n N` — the documented workaround is a FileLock + marker file). Velox's single process makes
one engine + N concurrent tests correct by construction. Say this in the README; it is the
clearest head-to-head win. (Measured: xdist `-n 4` was *slower* than serial on the 5000-test
suite — 4× startup + 4× collection + IPC exceeded the win.)

---

## 5. Execution model

One long-lived loop per process (`asyncio.Runner`, `loop_factory=uvloop` when available),
tests as tasks bounded by a semaphore (`--concurrency=N`, default 8–16 — the workload is
I/O-bound, so N ≫ cores, unlike xdist/jest). Fresh-loop-per-test is not on the table:
concurrency requires a shared loop by definition, and the shared loop *deletes* the entire
pytest-asyncio failure cluster ("attached to a different loop", "event loop is closed" at
teardown, `loop_scope` mismatch config) — all downstream of "loop per test, fixtures want to
be broader."

Isolation comes from four cheaper mechanisms:

1. **`TaskGroup` per test**, seeded into a ContextVar. Non-empty at test end ⇒ the test
   *fails* with the leaked coroutine's location. Converts "Task was destroyed but it is
   pending" shutdown noise into a named, actionable failure.
2. **Fresh `contextvars.Context` per test task** — per-test scoping that propagates to
   everything the test awaits/spawns and to nothing else. Same mechanism powers capture.
3. **Transactional DB isolation as the documented default** (SAVEPOINT-per-test rollback /
   connection-per-test on the shared engine).
4. **`--isolated` marker** → run in a subprocess on a fresh loop, for tests that touch
   signals, loop policy, `chdir`, or other process-globals. The documented answer for
   "my test is weird," instead of degrading everyone's model.

Pytest's phase model to keep/flatten: keep setup/call/teardown as internal phases (a teardown
failure after a passing call must surface; per-phase captured output is free), but emit **one
aggregated result per test** — pytest emits three reports per test and every consumer
re-aggregates by nodeid. Flatten the outcome model to a real enum:
`passed | failed | error | skipped | xfailed | xpassed | interrupted | timeout` — pytest
encodes xfail as a `hasattr(report, "wasxfail")` hack checked in five files, purely for
plugin back-compat velox doesn't owe. Adopt pytest's `ExitCode` values verbatim (0/1/2/3/4/5)
— every CI script branches on them, and `5 = no tests collected` catches the
"selector matched nothing, CI went green" footgun.

**An un-awaited coroutine test must be a failure.** Pytest's only async extension point is a
`trylast` hook plugins race to win; when ordering goes wrong the coroutine is never awaited
and the test silently *passes*. Velox: `RuntimeWarning: coroutine ... was never awaited`
during a test ⇒ test fails.

---

## 6. Scheduling, exclusive resources, determinism

**Explicit DI makes the deadlock problem disappear.** Every test's full resource footprint
(transitive closure of exclusive fixtures it depends on) is known statically before anything
runs. The scheduler admits a test only when its *entire* exclusive set is free and acquires
the set atomically — no hold-and-wait, therefore no deadlock, no lock-ordering discipline.
This one primitive subsumes xdist's whole scheduling zoo (`loadscope`/`loadfile`/
`loadgroup`/`worksteal` are all blunt proxies for "these tests conflict").

**Determinism = logical order vs physical order.**
- *Logical order*: stable collection order — sort by (path, definition order), stable
  parametrize ids (no set iteration, no unpinned `hash()`), a stable integer index per test.
- *Physical order*: greedy dispatch over logical order — take the next test whose exclusive
  set is free (aging rule so wide-footprint tests don't starve; seeded tiebreak).
- **All output is ordered by logical order**: failure details, short summary, JUnit XML.
  Result: byte-identical output run-to-run regardless of task timing — a stronger and more
  useful guarantee than reproducible interleaving (unachievable without a virtual clock),
  and it's what users actually mean by "deterministic."
- `maxfail`/`-x` defined over logical order: stop *dispatching* at N failures; cancel
  in-flight tests and report them `interrupted` (documented).

### Monkey-patching: the write is global, the view doesn't have to be

Python attribute resolution has no per-task indirection — modules and classes are process
singletons, so a naive `mock.patch`/`monkeypatch.setattr` is unavoidably global. But the
install can be separated from the override. Velox's `patch()` installs, once per target and
under a lock, a **router** at the patched slot — a proxy (module attribute) or descriptor
(class method) that consults a `ContextVar`: the current task's override if registered, else
the real object. Overrides live in the test task's context (inherited by child tasks),
teardown drops the entry — no global unwind, no LIFO-restore races. Method and module
mocking thereby become **task-local**; two tests can mock the same target differently,
concurrently. Routers are refcounted and removed when the last patcher finishes.

Honest limits, which define the fallback tiers: (1) `from m import f` aliases captured
before install — identical to pytest-monkeypatch's "patch where it's used" rule, no
regression; (2) `is`/`isinstance` see the proxy; (3) immutable/C targets (builtins, slotted
C types, `datetime.now` freezing) can't host a router; (4) threads velox never sees —
user-created executors, raw `threading.Thread`, library-owned worker threads — read
ContextVar defaults, so the router falls through to the *real* object there (a semantic
difference from pytest's global write, which threads do see). Note `asyncio.to_thread`
(stdlib copies context) and anything on the loop's default executor (velox installs a
context-propagating one — see §7 capture) are fine; SQLAlchemy's greenlet bridge stays on
the same thread/context. For the residue: the router detects reads from override-less
contexts while overrides are active and warns naming the patchers, and
`patch(global_=True)` escalates to the solo tier (real setattr, test runs alone) restoring
exact pytest semantics.

Where routing is impossible or the user reaches for raw `unittest.mock`, escalate to the
scheduler: a global patch is a **write lock against the whole suite** — the hazard is racing
a *non-patching* reader, so every normal test implicitly holds a read lock and a raw-global
patcher runs **solo** (drain, run alone, resume; aging prevents starvation; the deterministic
scheduler keeps it reproducible). `chdir`, signal handlers, and C-type patching escalate to
`--isolated` (subprocess).

The full ladder, strongest first — with automatic escalation, never silent unsafety:
**(a) DI override** — with explicit injection, most mocks are just a different callable
passed to one test: statically visible, zero magic, inherently local (FastAPI's
`app.dependency_overrides` is per-app-instance and already concurrency-fine if each
test/scope builds its own app) → **(b) routing `velox.patch`** → **(c) solo write-lock
execution** → **(d) `--isolated`**. The migration codegen should classify existing
`mock.patch`/`monkeypatch` sites: rewrite routable ones to `velox.patch`, mark the rest
solo, and suggest DI seams where the target is an injected dependency anyway.

---

## 7. Capture, tracebacks, reporting

### Capture: route by context, install once

Pytest's default capture is `os.dup2` on fd 1/2 into a shared temp file with a single global
slot and suspend/resume state asserts — structurally impossible under concurrency (the byte
stream contains no attribution information, even in principle). Velox:

- `sys.stdout/stderr = Router(real)` installed **once** at session start; `Router.write`
  looks up a per-test `Sink` in a ContextVar. Task context propagation attributes output from
  everything the test spawns.
- **One root logging handler for the whole session** whose `emit` writes to the current sink
  (strictly less code than pytest's add/remove-handlers-per-phase, and race-free). Retain
  structured records for a `caplog` equivalent.
- Honest documented limits: direct-fd writes from C extensions/subprocesses are
  unattributable → tee into a session-level "unattributed output" section; real fd capture
  only under `--isolated`/serial. Threads: `asyncio.to_thread` copies context by design
  (stdlib does `copy_context().run`), and velox installs a context-propagating **default
  executor** (`loop.set_default_executor`, `submit` wraps in `ctx.run` — correct because
  `run_in_executor` runs in the awaiting task's step, so submit-time context is the test's)
  so `run_in_executor(None, …)` and library helpers riding the default executor propagate
  too. Only user-created executors and raw `threading.Thread` fall through → their output
  lands in the unattributed section. `-s` prefixes each line with the test id or forces
  serial.
- `tmp_path`: allocate deterministically as `basetemp/<sanitized_test_id>` (uniqueness by
  construction — pytest's scan-and-retry numbering is a serial-era artifact). Keep the
  numbered session root + retention policy.

### Tracebacks: don't port `_pytest/_code`

It's ~3000 LOC and nearly standalone, but the judgment lives in config-coupled layers.
Stdlib 3.11+ (`TracebackException`, PEP 657 carets, ExceptionGroup rendering) + rich covers
it. Port exactly two ideas: **`__tracebackhide__`** (one dict lookup per frame; essential for
user assertion helpers) and **cut-to-the-test-function** (drop frames above the test via its
code object's path/firstlineno — two lines, disproportionate payoff). Async-specific must:
suppress `asyncio/`/`anyio/`/velox-runner frames, or every traceback drags in `Task.__step`.
Keep failure reprs as serializable dataclasses from day one (§8.8).

### Reporter: jest got it right, xdist shows the failure mode

Two regions on a tty:
- **Scrollback, atomic per file**: flush a file's block when all its tests finish
  (`PASS tests/api/test_users.py 12 tests, 0.84s`), internally coherent, ordering between
  blocks by completion.
- **Live footer (~10 Hz)**: progress + one line per in-flight test with elapsed time — this
  doubles as the hang-diagnosis UI and is something serial pytest structurally cannot offer.
- Failure details + short summary at the end in logical order (byte-identical);
  `--stream-failures` opt-in for long runs. Final line reports wall clock vs Σ durations —
  the proof-of-value metric. Non-tty/CI: no ANSI, one line per file, detail at the end;
  honor `NO_COLOR`/`FORCE_COLOR`/`CI`.
- Keep from pytest: the short test summary (`FAILED path::test - AssertionError: ...` — the
  most-copied line in pytest output), `--durations` (more important here: it drives
  concurrency tuning), the comparison-diff explanation engine (§2).
- JUnit XML: ~150 useful LOC. Testcases in logical order; honest wall-clock on
  `<testsuite time>` (CI dashboards sum per-test times and will otherwise call the fast
  suite slow — document the discrepancy). GitHub Actions `::error file=...` annotations are
  cheap and high-value.

---

## 8. Concerns not in the original list

1. **Loop-starvation watchdog — first release, not backlog.** One accidentally-blocking call
   (`requests`, sync DB driver, `bcrypt`) freezes the entire concurrent suite, undetectably
   from inside the loop. Watchdog thread + loop heartbeat; on stall: dump all task stacks +
   `faulthandler`, name the in-flight tests, warn or fail. "test_login blocked the event loop
   for 4.2 s — here's the stack" diagnoses a *production* bug and is an adoption reason in
   itself. Same thread writes the in-flight set to a file for hard-crash forensics
   (the concurrent replacement for `PYTEST_CURRENT_TEST`).
2. **Warnings filters are process-global.** `catch_warnings` save/restores a global list;
   concurrent restore clobbers. (3.14's context-aware warnings fixes this properly; can't
   rely on it for 3.11–3.13.) Collect globally with a `showwarning` shim attributing via the
   capture ContextVar; per-test `filterwarnings` marks honored only in serial/isolated mode —
   warn loudly, don't silently pretend.
3. **Ctrl-C choreography.** `loop.add_signal_handler(SIGINT)` → cancel the top TaskGroup →
   in-flight tests report `interrupted` → teardowns run under `asyncio.shield` with a hard
   timeout (leaked containers/schemas after Ctrl-C are a terrible first impression) →
   reporter flushes completed blocks → exit 2. Second Ctrl-C: `os._exit(2)`.
4. **Unraisables / task exceptions.** `sys.unraisablehook` + `loop.set_exception_handler`,
   attributed via the sink ContextVar where possible; leaked-task failure (§5) covers the rest.
5. **Cold-start economics.** Velox gets benchmarked cold in containers: rewrite-cache
   writability (§2), no entry-point scanning, one argv parse, lazy imports. Budget a startup
   target (<50 ms to first test) and CI-check it.
6. **Free-threading horizon.** All the ContextVar-based designs (capture, assertion globals,
   sinks) are also the correct answers under 3.13t+; avoid new module-global mutable state
   anywhere.
7. **Timeouts as first-class outcomes.** Per-test budget (`timeout` outcome) is near-free
   with tasks (`asyncio.timeout`), and pairs with the watchdog for sync-blocking cases.
8. **Report serializability from day one.** JSON-round-trippable results buy `--isolated`,
   future multi-process mode, `--report-json`, and testability. Retrofitting forced xdist's
   `Repr*` tree into pytest core — the cautionary tale.
9. **Scope discipline on plugins.** No hook system in v1; the escape hatch is explicit DI
   itself (fixtures are just callables users compose). Verify `coverage.py` works in CI from
   the first prototype. The migration codegen (pytest → velox) is a separate deliverable and
   the actual adoption bottleneck — name-based fixture graphs are statically resolvable from
   source + conftest scoping rules, which is exactly the analysis pytest does at runtime,
   done once at migration time.

---

## 9. Sizing

What velox is *not* building, from pytest's own inventory: collection core 8,450 LOC,
`_pytest` total 33,219 LOC + pluggy ~2k. What it vendors: rewriter + explanation engine
~2,400 LOC (plus their applicable upstream test intent). What it writes fresh, roughly:
discovery + flat test records (~300), DI resolver + static validator (~400), scope
lifecycle/refcount/single-flight (~500), scheduler (~300), capture router + log handler
(~200), watchdog + signal handling (~200), reporter (~600), JUnit/JSON output (~250),
CLI/config single-parse (~200). A v1 core in the ~3–4k LOC range plus vendored assertion
machinery is realistic.
