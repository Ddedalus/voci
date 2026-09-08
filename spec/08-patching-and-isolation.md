# 08 — Patching, Mocking, and Isolation Tiers

*The hardest concurrency problem in the design, because Python attribute resolution has no per-task
indirection: modules and classes are process singletons, so a naive `mock.patch` /
`monkeypatch.setattr` is unavoidably global (R§6). The insight that eventually rescues it: **the
write is global, but the view doesn't have to be** — the install can be separated from the override.
That insight is worth code only when it buys concurrency, which is why v0.1 ships it in exactly one
place — FastAPI's `dependency_overrides`, where the upstream read path is already dynamic (§3.1) —
and nowhere else.*

---

## 1. What voci does and does not own

**Mock *objects* are not the problem and voci does not replace them.** `MagicMock`, `AsyncMock`,
`create_autospec`, `mock.sentinel`, `assert_called_once_with` — all of it is per-instance state with
no process-global writes, so it is already concurrency-correct. Users keep `unittest.mock` and voci
never wraps it.

The problem is exclusively the **installer**: `mock.patch` / `monkeypatch.setattr` do a real
`setattr` on a module or class, which every concurrently-running test sees, and undo it on exit,
racing anyone who read the slot in between.

So the only question voci has to answer is: *what happens when a test installs a global override?*
Three answers, and they are the tiers.

## 2. The ladder

| Tier | Mechanism | Who builds it | Concurrency | Status |
|---|---|---|---|---|
| **(a) DI override** | Pass a different fixture or value at the call site | Nobody — it's just DI | Fully concurrent | **MVP** |
| **(a′) `voci.fastapi` layered client** | Tier (c)'s routing idea applied to exactly two attributes on one object the suite already owns | voci builds it, ~120 LOC | Fully concurrent | **MVP** (§3.1) |
| **(b) `unittest.mock`, scheduled solo** | Stock `mock.patch`; voci detects it and drains the suite | voci builds *detection + scheduling*, not patching | Serializes the suite around it | **MVP** |
| **(c) Routing `voci.patch`** | Router installed once at the slot; overrides live in the task's context | voci builds it | Fully concurrent | Roadmap (v0.2) |
| **(d) `@voci.isolated`** | Subprocess, fresh loop | voci builds the worker | Concurrent, but a process spawn | Roadmap |

voci picks the lowest tier that is *correct* for the target, escalates automatically when it can't
route, and always reports which tier a test used (visible in `-v` and the JSON report) so the cost
is never invisible.

**Why there is no `voci.patch` in v0.1.** If a patch API always runs solo, it is behaviourally
identical to `unittest.mock.patch` — a voci-branded wrapper with the same semantics, the same
speed, and a migration cost for no benefit. Shipping it would be pure API churn. `voci.patch`
earns its existence only at tier (c), where it does something `mock.patch` structurally cannot:
let two concurrent tests patch the same target differently. Until that exists, the honest answer is
"use `unittest.mock`, and voci will schedule your test safely" — which is also a much better
migration story, since it means untouched `mock.patch` call sites keep working.

Tier (a′) is not a counter-example to that rule. It is not a patch API and installs nothing at a
module or class slot; it replaces two attributes on a single object the test suite already holds a
reference to, whose upstream read path is dynamic by construction (§3.1). That is why 120 lines
suffice there and 300 plus a proxy/descriptor zoo do not suffice for the general case.

This resolves Q3 in the other direction from the first draft: **don't wrap, delegate.**

## 3. Tier (a): DI override — the intended default

With explicit injection, most mocks are just a different callable passed to one test: statically
visible, zero magic, inherently task-local. This is the tier the docs teach first and the migration
codegen tries hardest to reach.

```python
fake_http = MagicMock(spec=HttpClient)          # a stock mock object — fine, concurrent

@voci.fixture()
def service_with_fake_http() -> Service:
    return Service(fake_http)

async def test_retry(svc: Service = Depends(service_with_fake_http)):
    ...
```

(A method that derives `service_with_fake_http` from `service` automatically —
`service.with_(http=fake_http)` — is roadmap; [01](01-public-api.md) §10 has the design problem
that deferred it. The sibling-fixture form above needs no new API and is what tier (a) means
today.)

The reference stack has one seam where tier (a) does not reach, because the override point belongs
to the application object rather than to voci's DI graph: FastAPI's `app.dependency_overrides`.
That seam is specified in §3.1 and is the only place voci writes to an object it does not own.

### 3.1 `voci.fastapi`: layered `dependency_overrides` (MVP)

`app.dependency_overrides` and `app.state` are per-app-instance mutable dicts. The docs-blessed
pytest idiom — import the module-level `app`, assign `app.dependency_overrides[dep] = fake`, clear
it in teardown — is a process-global write under concurrency: two tests overriding the same
dependency clobber each other, and a non-overriding test racing the assignment sees the fake. The
obvious dodge, a `create_app(settings)` factory per test, is adoption-hostile: real FastAPI code is
singleton-shaped (`app = FastAPI()` at module level, routers included at import), and no team
rewrites production wiring to adopt a test runner. **voci therefore makes the override *view*
per-test while leaving the app object shared and the production code untouched.**

**Upstream facts the mechanism rests on.** All four are verified against the pinned FastAPI
submodule and the installed Starlette/httpx, and all four are pinned by assumption tests (below).

1. **Routes bake in a pointer to the app, not its overrides.** `FastAPI.__init__` passes
   `dependency_overrides_provider=self` into its router (`fastapi/applications.py`), and the
   pointer is propagated to every route at decoration/`include_router` time
   (`fastapi/routing.py`). The lookup itself is dynamic, per request, in `solve_dependencies`:
   `getattr(provider, "dependency_overrides", {}).get(original_call, original_call)`
   (`fastapi/dependencies/utils.py`). Replacing the attribute after routes exist is therefore seen
   by every subsequent request.
2. **FastAPI's entire contract with the attribute is a truthiness check plus `.get(key, default)`** —
   two adjacent call sites, no `in`, no iteration, no mutation. A read-only `Mapping` proxy with a
   truthy `__bool__` satisfies it exactly.
3. **`request.app` is the singleton and `app.state` is one object.** `Starlette.__call__` assigns
   `scope["app"] = self`; `State` is pure attribute delegation over a `_state` dict, constructed
   once and never reassigned upstream. So `state` can be layered the same way `overrides` is.
4. **httpx's `ASGITransport` awaits the app inside the calling task**, so each request inherits the
   test's `contextvars.Context` — which voci already makes fresh per test (I1). It also never
   sends a `lifespan` scope.

**Surface.** `voci.fastapi.client(app, *, overrides=None, state=None, base_url="http://testserver")`
— an async context manager yielding an `httpx.AsyncClient` over `ASGITransport(app)`. The public
shape and the canonical fixture live in [01](01-public-api.md) §3.

**Install once, per app object, idempotently.** On first entry for a given `app`, `client()`
replaces `app.dependency_overrides` with a `_LayeredOverrides(base=<the previous dict>)`, and — only
if `state=` is used — replaces `app.state` with a `State` subclass layering per-context values over
the existing `_state`. Subsequent entries see the proxy already installed and skip the swap. The
installation is a one-time process-wide effect with no teardown: the proxy is behaviourally
identical to the dict it replaced for any code holding no active layer.

**Layer routing.**

- **Read:** consult the `ContextVar` layer first, then `base`. Absent from both → the key is absent.
- **Write inside an active layer:** goes to the layer. This is what makes a hand-written
  `app.dependency_overrides[dep] = f` in the middle of a test concurrency-safe rather than merely
  tolerated.
- **Write outside any layer** (import time, a session fixture, app setup): goes to `base`, so
  process-wide overrides still behave as before.
- **Nesting:** entries stack; inner layers win key-by-key over outer ones, outer over `base`.
- **Values keep FastAPI's exact semantics** — an override is a dependency *callable*
  (`lambda: session`), never the value itself. voci does no auto-wrapping, because guessing would
  break every override whose replacement is itself callable.

**Escalation, not degradation (I6).** The pytest-docs teardown idiom `app.dependency_overrides = {}`
*replaces* the proxy, silently reverting the app to unlayered global state. voci detects this on the
next `client()` call — the attribute is no longer the installed proxy — and raises a loud, actionable
error naming the app, the likely teardown line, and the fix (delete the reset; overrides are scoped
to the `client()` block). It never reinstalls silently and never falls back to shared mutation.

**Lifespan.** `client()` never runs the app's lifespan — matching `ASGITransport`, which sends no
lifespan scope, and FastAPI's own documented warning that the test client does not trigger startup.
Suites needing real startup use `voci.fastapi.lifespan(app)`, a session-scoped fixture that runs the
lifespan exactly once per run; anything it writes to `app.state` lands in the **base** state, which is
the correct scope for a resource shared by the whole suite.

**Known limits, documented rather than papered over.**

1. Work **detached from the test's context** — `asyncio.create_task` from a background thread, a
   library worker pool, anything started outside the test's `Context` — reads the layer's default
   and sees only `base`. Same class of limit as tier (c) §5, and the same guidance applies.
2. `starlette.testclient.TestClient` (the sync thread-portal client) is **unsupported**: it runs the
   app in a portal thread with its own context. voci is async-first; the migration answer is
   `voci.fastapi.client`, not a shim.
3. The mechanism is per-**app-object**. A suite that genuinely builds several apps gets several
   independent installs, which is correct but means the escalation check is also per app.

**Assumption tests are a shipping requirement, not a nicety.** `tests/test_fastapi_layering.py`
must assert, against the real installed FastAPI/Starlette/httpx, each fact above, so that an upstream
change fails voci's own suite loudly instead of corrupting adopters' runs:

- `solve_dependencies` reads overrides dynamically per request (mutate after route registration →
  the new value is used);
- the only operations FastAPI performs on the attribute are a truthiness check and
  `.get(key, default)` (a proxy exposing nothing else still works end to end);
- `Starlette.__call__` sets `scope["app"] = self`, so `request.app` is the singleton;
- `State` delegates attributes to `_state` and upstream never reassigns `app.state`;
- `ASGITransport` runs the request in the caller's task/context and sends no lifespan scope.

**Rejected alternatives.**

| Alternative | Why rejected |
|---|---|
| Fresh-app factory per test (`create_app(settings)`) | Adoption-hostile — real code is singleton-shaped and prod wiring would have to change. Also repeats the per-route `Dependant` build and pydantic model construction on every test. |
| `copy.copy(app)` | Isolates nothing: the copied routes still carry `dependency_overrides_provider` pointing at the *original* app, so overrides resolve against the shared dict. |
| `copy.deepcopy(app)` | Slow at suite scale and breaks on the unpicklable objects real apps put in `state` (engines, clients, sockets). |
| Lock-serialized override mutation | Serializes the single most common fixture in the reference stack, which is precisely the concurrency voci exists to buy. |

**Cost and relation to tier (c).** ~120 LOC ([00](00-overview.md) §9), no proxy/descriptor
machinery, no refcounted global install/remove: it is the tier-(c) insight — *the write is global,
the view need not be* — applied to one attribute whose read path upstream already made dynamic.
General-purpose `voci.patch` stays deferred (§5).

**Migration bonus.** The docs-blessed idiom is mechanically recognizable (module-level `app` import +
`app.dependency_overrides[x] = y` + a reset in teardown) and rewrites 1:1 to
`voci.fastapi.client(app, overrides={x: y})`, which is the highest-value rewrite rule in
[12](12-migration.md) for the reference stack.

## 4. Tier (b): `unittest.mock` + solo scheduling — the MVP mechanism

Users write stock mock code:

```python
@mock.patch("myapp.clients.http_get", return_value=FAKE)
async def test_retry(http_get, svc: Service = Depends(service)):
    ...
```

voci's job is to notice and to schedule it safely.

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

1. **Runtime guard** — voci wraps `unittest.mock._patch.__enter__`. If it fires in a test that is
   not solo and not isolated, voci aborts that test and **requeues it as solo** (see below). Under
   `--strict-patch` it fails instead, with a message naming the target and pointing at
   `@voci.solo`.
2. **Codegen marking** — `voci migrate` finds these sites statically and adds `@voci.solo`
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

This is the design that makes mocking concurrent, and the only reason for voci to own a patch API.

**Mechanism.** `voci.patch` installs — once per target, under a lock, refcounted — a **router** at
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
4. **Threads voci never sees** — user-created executors, raw `threading.Thread`, library worker
   pools — read ContextVar *defaults*, so the router falls through to the **real** object there. A
   semantic difference from pytest's global write, which those threads *do* see.

   Fine, because context propagates on the paths that matter: `asyncio.to_thread` (stdlib does
   `copy_context().run`), anything on the loop's **default executor** (voci installs a
   context-propagating one — [09](09-capture-and-logging.md) §3), and SQLAlchemy's greenlet bridge
   (same thread, same context).

   For the residue, the router detects reads from override-less contexts *while overrides are
   active* and warns naming the patchers involved. `voci.patch(..., global_=True)` escalates to
   tier (b), restoring exact `mock.patch` semantics.

## 6. Tier (d): `@voci.isolated` (roadmap)

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

voci provides no `setenv` helper of its own — same reasoning as `voci.patch`: it would add an API
without adding a capability.

## 8. MVP

Tier (a) fully (it is just DI). Tier (a′): `voci.fastapi.client` / `voci.fastapi.lifespan`, the
layered overrides and state proxies, the replaced-proxy escalation, and `tests/test_fastapi_layering.py`
(§3.1) — required for the M2 gate, since the reference FastAPI suite cannot run concurrently without
it. Tier (b): static detection via `func.patchings`, the runtime guard on `_patch.__enter__`, the
scheduler write-lock ([06](06-scheduling-and-determinism.md) §3), and solo-cost reporting. Actionable
errors for the undetectable cases. The documented ladder, including what is coming.

**No general-purpose voci-owned patching code ships in v0.1.** The single exception is tier (a′),
which patches no global slot: it swaps two attributes on one application object, under rules the
upstream read path already permits.

## 9. Roadmap

- Tier (c): the routing `voci.patch` — module proxies, method descriptors, the override ContextVar,
  refcounted install/remove, the cross-context read detector. This is the ~300 LOC in the sizing
  budget, and it is spent only when it buys concurrency.
- Requeue-as-solo instead of failing, when the runtime guard fires.
- Tier (d): the `@voci.isolated` subprocess worker.
- Codegen rewriting of routable `mock.patch` sites to `voci.patch` ([12](12-migration.md)) — a pure
  performance migration, opt-in, never required.
- `voci.freeze_time`-style helpers built on tier (c) where the target permits (`datetime.now` is
  the canonical unroutable one and stays solo).

## 10. Open questions

- **Q3 — decided.** Delegate to `unittest.mock` and schedule solo; introduce `voci.patch` only when
  it is the routing tier. The remaining risk is unchanged — mocked tests serialize the suite in
  v0.1 — but users now pay it with code they already have, and the summary line makes the cost
  legible.
- **Q18** — Should the runtime guard on `_patch.__enter__` be on by default? It is a wrapper on a
  private stdlib attribute, which is a real fragility (`_patch` is not API). Proposed: on by
  default, guarded by a `getattr` check so a stdlib change degrades to "not detected" rather than a
  crash, plus a voci test that fails loudly on the next Python version if the hook stops working.
- **Q28** — Should a suite with *any* undetected global patching be given a way to say so
  wholesale — e.g. `solo_patterns = ["tests/legacy/**"]` in config — so migration can proceed
  coarsely before the sites are individually annotated? Cheap to add, and probably the difference
  between "we'll migrate eventually" and "we migrated on Friday".
