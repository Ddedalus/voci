# 08 — Patching, Mocking, and Isolation Tiers

*The hardest concurrency problem in the design, because Python attribute resolution has no per-task
indirection: modules and classes are process singletons, so a naive `mock.patch` /
`monkeypatch.setattr` is unavoidably global (R§6). The insight that eventually rescues it: **the
write is global, but the view doesn't have to be** — the install can be separated from the override.
That insight is worth code only when it buys concurrency, which is why v0.1 doesn't ship it.*

---

## 1. What velox does and does not own

**Mock *objects* are not the problem and velox does not replace them.** `MagicMock`, `AsyncMock`,
`create_autospec`, `mock.sentinel`, `assert_called_once_with` — all of it is per-instance state with
no process-global writes, so it is already concurrency-correct. Users keep `unittest.mock` and velox
never wraps it.

The problem is exclusively the **installer**: `mock.patch` / `monkeypatch.setattr` do a real
`setattr` on a module or class, which every concurrently-running test sees, and undo it on exit,
racing anyone who read the slot in between.

So the only question velox has to answer is: *what happens when a test installs a global override?*
Three answers, and they are the tiers.

## 2. The ladder

| Tier | Mechanism | Who builds it | Concurrency | Status |
|---|---|---|---|---|
| **(a) DI override** | Pass a different fixture or value at the call site | Nobody — it's just DI | Fully concurrent | **MVP** |
| **(b) `unittest.mock`, scheduled solo** | Stock `mock.patch`; velox detects it and drains the suite | velox builds *detection + scheduling*, not patching | Serializes the suite around it | **MVP** |
| **(c) Routing `velox.patch`** | Router installed once at the slot; overrides live in the task's context | velox builds it | Fully concurrent | Roadmap (v0.2) |
| **(d) `@velox.isolated`** | Subprocess, fresh loop | velox builds the worker | Concurrent, but a process spawn | Roadmap |

velox picks the lowest tier that is *correct* for the target, escalates automatically when it can't
route, and always reports which tier a test used (visible in `-v` and the JSON report) so the cost
is never invisible.

**Why there is no `velox.patch` in v0.1.** If a patch API always runs solo, it is behaviourally
identical to `unittest.mock.patch` — a velox-branded wrapper with the same semantics, the same
speed, and a migration cost for no benefit. Shipping it would be pure API churn. `velox.patch`
earns its existence only at tier (c), where it does something `mock.patch` structurally cannot:
let two concurrent tests patch the same target differently. Until that exists, the honest answer is
"use `unittest.mock`, and velox will schedule your test safely" — which is also a much better
migration story, since it means untouched `mock.patch` call sites keep working.

This resolves Q3 in the other direction from the first draft: **don't wrap, delegate.**

## 3. Tier (a): DI override — the intended default

With explicit injection, most mocks are just a different callable passed to one test: statically
visible, zero magic, inherently task-local. This is the tier the docs teach first and the migration
codegen tries hardest to reach.

```python
fake_http = MagicMock(spec=HttpClient)          # a stock mock object — fine, concurrent

async def test_retry(svc: Service = Depends(service.with_(http=fake_http))):
    ...
```

FastAPI's own `app.dependency_overrides` is per-app-instance and is already concurrency-safe *if
each test or scope builds its own app* — the recipes page must show that, because sharing one app
instance across concurrent tests with mutated overrides is the most likely footgun in the reference
stack.

## 4. Tier (b): `unittest.mock` + solo scheduling — the MVP mechanism

Users write stock mock code:

```python
@mock.patch("myapp.clients.http_get", return_value=FAKE)
async def test_retry(http_get, svc: Service = Depends(service)):
    ...
```

velox's job is to notice and to schedule it safely.

### Detection

**Decorator form is detectable statically, at zero cost.** `unittest.mock`'s
`_patch.decorate_callable` sets `func.patchings = [self]` on the wrapper (verified against CPython's
`unittest/mock.py`), and appends for stacked patches. So collection does one `hasattr(func,
"patchings")` per test and, when true:

- marks the test solo;
- reads `p.target` / `p.attribute` off each patcher for the report, so the run can say *what* is
  being patched and why the suite drained.

**Context-manager form** (`with mock.patch(...)` inside a test body) is not visible from the
function object. Two mechanisms cover it:

1. **Runtime guard** — velox wraps `unittest.mock._patch.__enter__`. If it fires in a test that is
   not solo and not isolated, velox aborts that test and **requeues it as solo** (see below). Under
   `--strict-patch` it fails instead, with a message naming the target and pointing at
   `@velox.solo`.
2. **Codegen marking** — `velox migrate` finds these sites statically and adds `@velox.solo`
   ([12](12-migration.md)), so a migrated suite is correctly annotated before it ever runs.

*Requeue-as-solo is the nicer behavior and is roadmap*: it needs abort-and-rerun semantics (drop the
partial result, release fixtures, re-dispatch under the write lock) and a guard against a test that
patches conditionally and so re-aborts forever. MVP behavior is to fail with an actionable message;
the detection is the part that must exist from day one, because the alternative is a silent race.

### Scheduling

A global patch is a **write lock against the whole suite**. The hazard is racing a *non-patching
reader*, so every normal test implicitly holds a read lock and a patching test runs solo: drain,
run alone, resume. Aging prevents starvation and the deterministic scheduler keeps it reproducible
([06](06-scheduling-and-determinism.md) §3).

Cost is reported explicitly — number of solo tests and wall-clock spent drained — because a suite
that drifts into 200 solo tests has lost the parallelism and should see that in the summary rather
than in a stopwatch.

## 5. Tier (c): the routing patch (roadmap)

This is the design that makes mocking concurrent, and the only reason for velox to own a patch API.

**Mechanism.** `velox.patch` installs — once per target, under a lock, refcounted — a **router** at
the patched slot: a proxy object for a module attribute, a descriptor for a class member. The router
consults a `ContextVar` on access: the current task's override if one is registered, else the real
object. Overrides are written into the *test task's* context (inherited by child tasks), and
teardown drops the entry — **no global unwind, no LIFO-restore race**. Routers are removed when the
last patcher for that target finishes.

Result: two tests can mock the same target differently, at the same time. Nothing in pytest can do
that at any concurrency.

### Honest limits (these are why the other tiers still exist)

1. **`from m import f` aliases captured before install** are not routed. Identical to
   pytest-monkeypatch's "patch where it's used" rule — no regression, same guidance.
2. **`is` / `isinstance` see the proxy.** Documented; escalating to tier (b) is the out.
3. **Immutable or C-level targets** (builtins, slotted C types, `datetime.now`) cannot host a
   router → auto-escalate to (b) or (d).
4. **Threads velox never sees** — user-created executors, raw `threading.Thread`, library worker
   pools — read ContextVar *defaults*, so the router falls through to the **real** object there. A
   semantic difference from pytest's global write, which those threads *do* see.

   Fine, because context propagates on the paths that matter: `asyncio.to_thread` (stdlib does
   `copy_context().run`), anything on the loop's **default executor** (velox installs a
   context-propagating one — [09](09-capture-and-logging.md) §3), and SQLAlchemy's greenlet bridge
   (same thread, same context).

   For the residue, the router detects reads from override-less contexts *while overrides are
   active* and warns naming the patchers involved. `velox.patch(..., global_=True)` escalates to
   tier (b), restoring exact `mock.patch` semantics.

## 6. Tier (d): `@velox.isolated` (roadmap)

Subprocess on a fresh loop, one test, result shipped back as JSON (possible only because of I4).
Required for `chdir`, signal handlers, loop-policy changes, C-type patching, `sys.settrace`-style
global instrumentation, and anything that mutates interpreter state irreversibly. Unlike tier (b) it
does **not** take the write lock — it shares no process state, so it runs concurrently with
everything else.

## 7. Environment variables and `chdir`

`monkeypatch.setenv` is the most common monkeypatch use in real suites and deserves an explicit
answer, since `os.environ` is a process global that libraries read at arbitrary times.

- **Preferred:** inject configuration (a settings fixture), which most FastAPI apps already have via
  pydantic-settings. Tier (a), fully concurrent.
- **`mock.patch.dict(os.environ, ...)`** works and is detected like any other patch → solo.
- **`chdir`** always escalates to `--isolated`. There is no per-task cwd in CPython.

velox provides no `setenv` helper of its own — same reasoning as `velox.patch`: it would add an API
without adding a capability.

## 8. MVP

Tier (a) fully (it is just DI). Tier (b): static detection via `func.patchings`, the runtime guard
on `_patch.__enter__`, the scheduler write-lock ([06](06-scheduling-and-determinism.md) §3), and
solo-cost reporting. Actionable errors for the undetectable cases. The documented ladder, including
what is coming.

**No velox-owned patching code ships in v0.1.**

## 9. Roadmap

- Tier (c): the routing `velox.patch` — module proxies, method descriptors, the override ContextVar,
  refcounted install/remove, the cross-context read detector. This is the ~300 LOC in the sizing
  budget, and it is spent only when it buys concurrency.
- Requeue-as-solo instead of failing, when the runtime guard fires.
- Tier (d): the `@velox.isolated` subprocess worker.
- Codegen rewriting of routable `mock.patch` sites to `velox.patch` ([12](12-migration.md)) — a pure
  performance migration, opt-in, never required.
- `velox.freeze_time`-style helpers built on tier (c) where the target permits (`datetime.now` is
  the canonical unroutable one and stays solo).

## 10. Open questions

- **Q3 — decided.** Delegate to `unittest.mock` and schedule solo; introduce `velox.patch` only when
  it is the routing tier. The remaining risk is unchanged — mocked tests serialize the suite in
  v0.1 — but users now pay it with code they already have, and the summary line makes the cost
  legible.
- **Q18** — Should the runtime guard on `_patch.__enter__` be on by default? It is a wrapper on a
  private stdlib attribute, which is a real fragility (`_patch` is not API). Proposed: on by
  default, guarded by a `getattr` check so a stdlib change degrades to "not detected" rather than a
  crash, plus a velox test that fails loudly on the next Python version if the hook stops working.
- **Q28** — Should a suite with *any* undetected global patching be given a way to say so
  wholesale — e.g. `solo_patterns = ["tests/legacy/**"]` in config — so migration can proceed
  coarsely before the sites are individually annotated? Cheap to add, and probably the difference
  between "we'll migrate eventually" and "we migrated on Friday".
