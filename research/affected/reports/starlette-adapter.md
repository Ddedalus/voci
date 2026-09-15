# Starlette/FastAPI adapter: request recording, route table, soundness on 16 edits

> Subagent report from the 2026-09-12 research pass, kept verbatim. Paths under `/tmp` were rewritten to their copies in `research/testmon/`; the venvs they ran in weren't kept (see `../README.md`).

## Task 1 — Request recording and attribution

Patched `starlette.routing.Router.__call__` once at import (`research/testmon/starlette-adapter/adapter.py`), keyed on a `ContextVar[Collector]`. Recording happens on the *outermost* call only (`scope.get("_voci_recorded")` guard), reading `scope["path"]`/`scope["method"]` before dispatch — this survives arbitrary `Mount` nesting because `scope["path"]` is never rewritten (only `root_path` is), and `scope` is one dict mutated in place through every nested `Router`/`Mount` call.

All required scenarios passed (`research/testmon/starlette-adapter/scripts/recorder_probe.py`): module-level `TestClient(app)`, a persistent `with TestClient(app) as c` used across several "tests", two `httpx.AsyncClient(transport=ASGITransport(app))` tasks interleaved via `asyncio.gather` + a `Barrier`, two sync tests driven from two OS threads, `websocket_connect`, and bare Starlette (not just FastAPI — same `Router.__call__` is used by both).

One real failure mode reproduced deliberately: a bare `threading.Thread(target=...)` that does **not** set its own collector sees `current_collector.get() is None` — contextvars do not cross thread creation (confirms the plan's existing finding; the fix is the plan's already-designed context-propagating `Thread.start`, not something this adapter needs to add).

**Coordinator follow-up (matched-but-unexecuted routes):** extended the recorder to also capture, in a `try/finally` around the dispatch, `scope.get("route")` (FastAPI's `APIRoute`) or `scope.get("endpoint")` into `Collector.matched_routes`. Verified empirically that this recovers the route even when the handler body never runs:

```
method-405   status=405 matched_routes=['search']     # PARTIAL match sets scope["endpoint"] before Route.handle() 405s
body-422     status=422 matched_routes=['create_order'] # pydantic rejects body before create_order() is called
dep-raises-401 status=401 matched_routes=['protected']   # Depends(require_token) raises before protected() runs
plain-404    status=404 matched_routes=[]              # Match.NONE: no route object exists, genuinely unrecoverable
```
Root cause confirmed by reading `starlette/routing.py`: both `Route.matches()` (FULL and PARTIAL) and `Router.app()`'s partial-match branch set `scope["endpoint"]`/call `scope.update(child_scope)` **before** `route.handle()` runs, so it survives any exception the handler pipeline raises. **True 404s are the only unrecoverable case** — there's no route to attribute to, but that's fine: 404s are exactly what the (method, path) request record + old/new pattern matching (Task 3) already handles.

## Task 2 — Route table extraction

`adapter.extract_routes()` walks `fastapi.routing.iter_route_contexts()` when available (this FastAPI version — 0.141.1 — lazily flattens `include_router` through `_IncludedRouter`/`_EffectiveRouteContext` rather than eagerly copying routes), recursing manually into `Mount` sub-apps (which `iter_route_contexts` does not cross). Falls back to a plain recursive walk of `.routes` for bare Starlette (which has no `include_router` at all).

Built `research/testmon/starlette-adapter/app/` with routers in separate files, `APIRouter(prefix=...)`, multi-level `include_router(prefix=...)` (including a prefix containing a path param), a router nested inside another router, a `Mount` of a sub-`FastAPI`, `{id:int}` converters, `api_route(methods=[...])`, `add_api_route`, a websocket route, and a non-literal (`DYNAMIC_PATH = "/" + ...`) path. All resolved correctly, e.g. `/users/{user_id}/items/{item_id:int}` and `/api/admin/reports/detail/{report_id:int}` came out exactly right, converters intact.

Two real bugs found and fixed while validating against the runtime table:
- `_EffectiveRouteContext.endpoint`/`.path`/`.path_format`/`.methods`/`.name` are only populated for the plain-`Route` branch; `Mount`/`WebSocketRoute`/`Host` leave them at dataclass defaults and put the real values on a nested `starlette_route` — my first pass silently dropped the websocket route entirely. Fixed with a `_resolved()` helper that checks `starlette_route` → `rc`'s own fields → `original_route`, in that order.
- Only `path_format` (converter-stripped, e.g. `{id}`) was captured initially; added a separate `path` field carrying converters (`{id:int}`) since selection-time matching needs to be converter-aware (see shadow test below).

**What can't be mapped:** FastAPI's own built-in routes (`/openapi.json`, `/docs`, etc.) map to `fastapi/applications.py` — outside rootdir, so trivially filterable as non-first-party. A `Mount` of a non-Starlette ASGI app has no `.routes` to recurse into — reported as `kind="mount-opaque"`, full coverage requires falling back to coarse for anything under that prefix.

## Task 3 — Static side

`research/testmon/starlette-adapter/staticparse.py` parses decorators (`get/post/put/patch/delete/head/options/api_route/route/websocket`), `add_api_route`/`add_route` calls, and `include_router`, capturing the receiver (only a simple `Name` is trusted — an `Attribute` receiver like `items.router` is flagged non-simple and forced coarse), the literal path (`None` if not an `ast.Constant` — the "path from a variable" case), methods, and every other decorator kwarg (`ast.dump`'d, for a behavioral/OpenAPI-only diff later).

**Deriving a new route's full path:** find a sibling route already in the *old* route table registered from the same `(file, receiver)`, and subtract that sibling's own literal source path as an exact string suffix from its resolved `path` (both converter-preserving, so no `compile_path` round-trip needed). Verified: a new `@router.get("/{item_id:int}/reviews")` in `items.py` correctly resolves to `/users/{user_id}/items/{item_id:int}/reviews` (3 levels of prefix composition); a new route on `admin.py`'s nested `reports_router` resolves to `/api/admin/reports/new-report`; the non-literal `dynamic.py` route correctly reports "can't derive statically."

One subtlety fixed: a file can hold two receivers with different prefixes (`admin.py`'s `router` and its nested `reports_router`) — the sibling search now prefers a same-receiver sibling before falling back to any sibling in the file, to avoid silently borrowing the wrong prefix.

**"Same route across an edit":** identity = `(file, endpoint_qualname)`, reusing the plan's existing def-block identity rather than inventing a second concept. Kwarg changes are split into `OPENAPI_ONLY_KWARGS = {tags, summary, description, deprecated, operation_id, include_in_schema, responses, openapi_extra}` (schema-only, no runtime effect) vs. everything else (`response_model`, `status_code`, `dependencies`, methods, path — "behavioral").

## Task 4 — Empirical soundness

32 tests in `research/testmon/starlette-adapter/tests/test_app.py` (request tests, 404/405, a 422 body-validation test, a 401 dependency-raise test, an OpenAPI snapshot, a `url_path_for` test, a websocket test, control tests). `research/testmon/starlette-adapter/scripts/run_edits.py` applies each edit via git-revertible source substitution, runs the suite before/after (outcome diff = ground truth), extracts old/new route tables + static parses in fresh subprocesses, and checks predicted ⊇ actual.

| Edit | Changed tests ⊆ predicted? | Predicted | Coarse |
|---|---|---|---|
| Add unrelated route | ✓ (0 changed) | 2 | 32 |
| Shadow: drop `:int` converter so `/users/{id}` shadows `/users/me` | ✓ | 6 | 32 |
| Existing path, new method (405→200) | ✓ | 5 | 32 |
| Change a route's path | ✓ | 3 | 32 |
| Change `response_model` | ✓ (0 changed) | 4 | 32 |
| Change `status_code` (via `add_api_route`) | ✓ | 4 | 32 |
| Change `dependencies=[...]` | ✓ (0 changed) | 4 | 32 |
| Change `tags`/`summary` | ✓ (0 changed) | 2 | 32 |
| Remove a route | ✓ | 4 | 32 |
| Rename a route (`name=`) | ✓ | 5 | 32 |
| Add middleware | ✓ (0 changed) | 2* | 32 |
| Add exception handler | ✓ (0 changed) | 2* | 32 |
| Change `include_router` prefix | ✓ | 5 | 32 |
| `Field(gt=0)→gt=5` on a body model | ✓ | 7 | 32 |
| Change a query param's default | ✓ | 7 | 32 |
| Dependency's raise condition changed | ✓ | 7 | 32 |

**16/16 sound.** `*` — middleware/exception-handler predictions are an artifact of a harness proxy, not a real adapter guarantee; see edge cases below.

Two bugs only surfaced by actually running this: forgot to regenerate the cached "before" state after fixing the static parser (stale JSON, `add_api_route`'s kwargs and endpoint-qualname resolution silently didn't match); and the file-scoped fallback (below) was initially unconditional, which regressed precision on the "unrelated route" case by dragging in every other test sharing that file.

## Edge cases forcing the coarse fallback

- **Non-literal paths / non-simple receivers** — can't derive a new route's path or safely attribute its prefix (by design).
- **Pure reordering** (two routes' relative order swaps with no other attribute change, e.g. fixing the legacy shadow by moving `/health` before `/{name}`) — invisible to a per-route diff since nothing about either route's own content changed. `selection.detect_pure_reorder()` flags it and falls back to "all tests using the app." In the real system this is actually already over-covered for free: reordering changes the *module* block's AST dump (statement order), so rule 3's import-closure already reselects every test importing that file — the adapter doesn't reduce this, and shouldn't pretend to.
- **App-level effects** (middleware, exception handlers, lifespan, `dependency_overrides`) — genuinely need "every test using the app," not a file-scoped guess. My harness's `matched_routes`-in-changed-file proxy under-counted these (2 instead of 32) purely because no test in the suite happened to assert on the injected header/exception; this is a limitation of the proxy, not evidence that middleware is narrowable — flagging this loudly so it isn't misread.
- **A body model's field constraint, a handler's own parameter default, or a `Depends()` function's body** — none of these are decorator kwargs, so the route-table/kwarg diff alone finds nothing. This is legitimately the coordinator's ask: in the real system, rule 1 (block actually traced) or rule 3 (module-block import closure, coarse) already covers it; the adapter's marginal contribution is only to *narrow* rule 3's coarseness using `matched_routes`. My harness approximates this with a "file changed + matched_routes touches that file" fallback, gated to only fire when the route/kwarg diff found nothing (otherwise it regresses precision on ordinary route edits).
- **Opaque `Mount`** of a non-Starlette ASGI app — no routes to recurse into, forced coarse for that whole prefix.

## Task 5 — Cost and robustness

- **Overhead:** raw ASGI-level benchmark (`scripts/bench_overhead2.py`, bypassing TestClient/httpx entirely): unpatched FastAPI dispatch ≈161.5µs/request; patched with active recording ≈164.2µs (~1.7%); patched with no collector ≈164.1µs (basically free). First cut was ~42% overhead (68µs) because `_map_endpoint()` called `inspect.getsourcelines()` — a full file re-read/re-parse — on *every* request; caching by `id(endpoint)` (endpoints are held alive by the route table for the app's lifetime) fixed it. Worth calling out since it would have been a materially wrong finding without the direct-ASGI benchmark forcing the issue.
- **Starlette internals stability:** cloned `encode/starlette`. `compile_path`'s signature is stable since 2019 (`2f51a38`); `Route.matches`'s behavior last changed substantively in 2019 (`06adecd`, lifespan routes), touched only cosmetically since (`__future__.annotations` reformatting, 2024). `Router.__call__` has been a one-line delegation to `self.middleware_stack` since at least the "middleware per Router" change (Dec 2023) and is untouched on `origin/codex/add-route-index-api` (an *unmerged*, as-of-today trie/route-index branch) — that branch replaces the linear `for route in self.routes` scan with a trie-prefiltered `self._route_index.candidates(...)`, but preserves `scope.update(child_scope)` and *additionally* sets `scope["route"] = route` unconditionally (today only FastAPI's `APIRoute` path does that) — i.e. our hook point and the `scope["route"]`/`scope["endpoint"]` contract look durable through this pending change, and the `route`-fallback path would get even more reliable if it lands.
- **Other frameworks** (not prototyped, just the hook shape): **Litestar** — its own ASGI router (`HTTPRoute`/`WebsocketRoute`), route resolution comparable to `Router.app`; **Django** — WSGI/ASGI via `URLResolver.resolve()`, matched view on `resolver_match.func`; **Flask** — WSGI via Werkzeug's `MapAdapter.match()`, which raises `MethodNotAllowed`/`NotFound` but still exposes the matched `Rule` for a 405. Each would need its own adapter; none share Starlette's ASGI scope/route-table shape.

## Recommendation and effort estimate

The approach is sound and the overhead is negligible once the endpoint-mapping cache is in place. Recommend building it, scoped narrowly: route add/remove/path/method changes matched via `compile_path` FULL-or-PARTIAL against recorded `(method, path)`; `matched_routes` reselection for behavioral kwarg changes (and, pending M1's real tracer, standing in for "the def block that would've been the natural block dependency" for handlers blocked by validation/dependencies); `used_route_table` coarse reselection for `openapi()`/`url_path_for`; explicit coarse fallback (not a guess) for non-literal paths, non-simple receivers, opaque mounts, and app-level effects. Reordering doesn't need special handling — the base module-block rule already covers it.

Rough effort for a production version: route table extraction + recorder ≈ 1–2 days (the FastAPI lazy-flattening quirks were the only real surprise); static parser + prefix derivation ≈ 1–2 days; wiring `matched_routes` into the real fingerprint/dependency-set machinery (not just a harness proxy) is the biggest unknown and probably the bulk of the work, since it means teaching the M2 block/dependency system a synthetic "matched but not traced" dependency kind; tests ≈ 1–2 days. Call it a good week, with the fingerprint integration as the main risk.

## Script paths
- `research/testmon/starlette-adapter/adapter.py` — recorder + route table extraction
- `research/testmon/starlette-adapter/staticparse.py` — static registration parser + prefix derivation + kwarg classification
- `research/testmon/starlette-adapter/selection.py` — the predictor (route diff, kwarg diff, reorder detection)
- `research/testmon/starlette-adapter/app/` — synthetic FastAPI app (`main.py`, `sub.py`, `routers/{items,users,legacy,admin,multi,ws,dynamic,validated}.py`)
- `research/testmon/starlette-adapter/tests/{conftest.py,test_app.py}` — 32-test suite with the collector/outcome dump fixture
- `research/testmon/starlette-adapter/scripts/run_edits.py` — the edit/soundness harness (main deliverable for Task 4)
- `research/testmon/starlette-adapter/scripts/{recorder_probe.py, smoke_route_table.py, smoke_staticparse.py, smoke_recorder.py}` — Task 1–3 probes
- `research/testmon/starlette-adapter/scripts/{bench_overhead.py, bench_overhead2.py}` — Task 5 overhead benchmarks
- `research/testmon/starlette-adapter/starlette-src/` — cloned starlette history for the stability check
