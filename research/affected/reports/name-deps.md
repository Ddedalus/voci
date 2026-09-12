# Per-statement (name-level) module dependencies: analyzer, soundness cases, precision on fastapi/httpx

> Subagent report from the 2026-09-12 research pass behind `plans/testmon-plan.md`, kept verbatim. Paths under `/tmp` were rewritten to their copies in `research/testmon/`; the venvs they ran in weren't kept (see `../README.md`).

## Summary

Built and measured a prototype name-level (per-top-level-statement) module dependency analyzer against the testmon-plan design. Scripts are under `research/testmon/name-deps/`; nothing in `/home/hubert/voci` was touched.

**Script paths:**
- `research/testmon/name-deps/namedeps.py` — the analyzer (957 lines)
- `research/testmon/name-deps/proj/` — synthetic first-party project (`pkg/`, `tests/test_cases.py`) for soundness cases
- `research/testmon/name-deps/soundness.py` — soundness harness (edits a file, rebuilds, diffs block hashes, checks closure membership)
- `research/testmon/name-deps/precision.py` — fastapi measurement
- `research/testmon/name-deps/precision_httpx.py` — httpx measurement
- `research/testmon/name-deps/.venv-deps/` — throwaway venv with pydantic/sqlalchemy (only needed so the synthetic project's imports are syntactically real; the analyzer itself never executes code, only `ast.parse`s)

## 1. Analyzer design

Per module: one `Block` per `ast.Module.body` statement, recording `binds`, `refs` (bare names / dotted chains rooted at a name / string literals), `is_effect`, and — only for `FunctionDef`/`AsyncFunctionDef` — a separate `body_refs` (full body walk) used only when a def-block is *resolved-to* (i.e. "this got called"), keeping the block's own `refs` (signature+decorators only) for *defining* it. This split turned out to matter a lot (see part 3).

Key mechanisms:
- **Cross-module resolution**: `from x import y` chases into `y`'s real binding in `x` (recursively through re-export chains), disambiguating "`y` is itself a submodule" vs "`y` is a name" by checking the discovered module table. Relative imports resolved via level+package-dotted-name arithmetic. `import a.b.c` (dotted, unaliased) walks submodule-by-submodule as chains are dereferenced.
- **`import *`**: resolved *precisely* rather than falling back — binds each of the source module's public names (`__all__` if present, else non-underscore top-level names) as individual aliases. (The prompt said "decide which"; precise beat fallback since it was equally cheap to implement.)
- **Effect folding**: an effect statement's narrow reference set is resolved (without the string heuristic, to avoid noise) to concrete first-party targets; the effect block is indexed under each target `BlockKey`/module and pulled into the closure whenever that target is reached — cross-module, so `router_file.py: @app.get(...)` folds correctly onto `app`'s assignment in a *different* file that `router_file.py` imported it from. Verified in soundness case 8.
- **Decorator-factory widening** (added after finding it necessary, see §2): one-hop interprocedural look-through — if a decorator is a call to a first-party factory function, and that factory's body mutates an enclosing-scope name via subscript/attribute (`REGISTRY[name] = fn`), the effect also folds onto whatever that mutated name resolves to. Without this, registry-populating decorators are **unsound** under the name-level scheme (see §2, case 6).
- **String-literal heuristic**: constants matching `identifier(.identifier)*`, minimum 3 chars, filtered through a small stopword list (`id`, `name`, `type`, …), matched by last dotted segment against a global index of all first-party top-level names.
- **Whole-module fallback**: triggered by module-level `__getattr__` (PEP 562), module-level `exec`/`eval`/`globals`/`vars` calls, or a module object referenced bare (passed as a value rather than `.attr`-accessed). Found and fixed a real bug here: initially, reaching `WholeModule(M)` in the closure only pulled in `M`'s own literal statements, not what `M`'s own imports re-export — for a re-export hub that's almost nothing. Fixed by expanding a `WholeModule` hit into `resolve_binding` (fallback-bypassing variant) for every name `M` binds.
- **`from __future__ import annotations`**: turned out to be a non-issue — PEP 563/649 change runtime `__annotations__` stringification, not the AST; annotation nodes are full expression trees either way, so no special-casing was needed.

**Constructs explicitly punted on** (see inline `# GAP` and code comments):
- Function-scoped/nested `import` statements aren't parsed into any block's `import_aliases` (block splitting only looks at `tree.body` top level) — a name bound by a nested import is invisible unless coincidentally also bound at module top level.
- A plain `import x` whose bound name is *never referenced again anywhere* — its module's global effects never enter any closure, since nothing ever asks to resolve `x`. (Executing the import always runs `x`'s top level in reality.)
- Assignment to a plain name whose RHS mutates some *other* referenced object in place (`_ = configure(app)`) is classified as a pure binding, not an effect — `app`'s mutation is invisible.
- Attribute-chain resolution follows only one hop past a `from X import Y as base` where `Y` isn't itself a recognized submodule; further `.attr` accesses on the result aren't chased.
- Walrus-bound names (`:=`) and `match`/`case` capture patterns at module top level aren't tracked by `collect_bound_names` (both are rare at that scope).
- `# type:` comments are ignored (not in the AST without `type_comments=True`); irrelevant on modern pydantic v2 code.
- `importlib.import_module(dynamic_name)` — expected and confirmed gap (§2, §3).
- SQLAlchemy `ForeignKey("table.col")` by table name — expected and confirmed miss (§2).

## 2. Soundness cases (synthetic project, `research/testmon/name-deps/proj`)

All 21 cases run via `soundness.py`, each doing a real edit, rebuild, and closure-membership check (not a hand-picked assertion).

| Case | Caught? | How | If missed, why / what would catch it |
|---|---|---|---|
| Constant via `from config import TIMEOUT` | Caught | direct name resolution | — |
| Constant computed from another constant, read via `config.TOTAL_BUDGET` | Caught | transitive closure over a block's own `refs` | — |
| Same, editing the *other* upstream constant (`RETRIES`) | Caught | same | — |
| Type alias (`Annotated[..., Field(...)]`) used in a model field | Caught | annotation reference in class block's head-refs | — |
| PEP 695 `type X = ...` alias used in a model field | Caught | same | — |
| Pydantic: add/change a field on a nested model | Caught | whole class is one block; class-as-type reference from the outer model | — |
| Pydantic: change a `Field(...)` constraint | Caught | same block | — |
| Pydantic: change `model_config` | Caught | same block | — |
| Pydantic: forward reference by string (`"Manager \| None"`) | Caught | annotation string parsed via `ast.parse(mode='eval')`, `Manager` resolved from the result | — |
| Pydantic model used only as a handler's parameter annotation (validation is pydantic-core, no first-party frames) | Caught | the handler's *signature* is part of its def-block `refs`; a def-block that's merely referenced (called) pulls in its signature refs regardless of what actually executes inside pydantic-core | Notable: this is a case where the **static** signature reference beats a pure runtime tracer, which would see no first-party frame at all |
| Dataclass field default change | Caught | class block | — |
| Enum member added | Caught | class block reached via `Widget.color: Color` | — |
| Base class change | Caught | class block (bases walked) | — |
| `__slots__` | Caught | part of the class block (ordinary assignment inside it) | — |
| Re-export hub: unrelated statement in `routing.py` | **Missed (correctly)** | `pkg.Router` only pulls in `Router`'s own block | This is the point of the refinement — confirms it's finer than file-level |
| Re-export hub: edit `Router`'s class body | Caught | same resolution path | — |
| Registry: change an existing `@register("key")` string | Caught **only with the decorator-factory widening heuristic** | one-hop interprocedural fold of `register()`'s closure mutation onto `REGISTRY` | **Without the heuristic: MISSED** — the decorator's own direct reference is to `register` (the factory), not to `REGISTRY`; verified empirically by disabling the heuristic |
| Registry: add a brand-new registration | Caught | REGISTRY is one mutable object; any test resolving `REGISTRY` picks up every registration onto it (conservative, sound, same coarseness as file-level for this one dict) | — |
| Brand-new undecorated function added | Correctly caught by nothing | no test references it | This is the main precision win over file-level: a new function no longer invalidates its file's whole test fan-in |
| New decorated route referencing `app`, added in a *different* file that imports `app` | Caught | cross-module effect-fold onto `app`'s assignment block | — |
| `importlib.import_module(f"pkg.plugins.{name}")` | **Missed (expected)** | — | module name built at runtime from an f-string; no static string or attribute chain names it. Confirmed this isn't a synthetic-only concern: **156/578 (27%) of fastapi's own test files** use exactly this pattern (`importlib.import_module(f"docs_src.x.{param}")` in parametrized fixtures) to load tutorial example modules |
| `if TYPE_CHECKING:` import / `try/except ImportError` conditional def / `__all__` / `del` | Caught | each compound statement is one block; `del`/`__all__` treated as ordinary effect/binding statements | — |

Also confirmed directly (not via a test, via direct resolver query) on a small SQLAlchemy-shaped example: `relationship("AddressTable")` **is** caught (string matches a class name by last segment); `ForeignKey("addresses.id")`'s `"addresses.id"` **is not** — its last segment `"id"` isn't a bound top-level name anywhere, and `__tablename__ = "addresses"` (a snake_case string) has no textual relationship to the class name `AddressTable` at all. Confirms the task's own prediction.

## 3. Precision on `oss/fastapi`

Built over `fastapi/` (48 modules, 740 top-level statements), `tests/` (578 test modules — `find -name test_*.py` undercounts at 492 because ~86 fastapi tests are organized as `test_x/__init__.py` packages), and `docs_src/` (needed because most tutorial tests reach `fastapi` only through their docs_src example, not directly).

**Zero modules** in the whole 1102-module tree triggered whole-module fallback (no `__getattr__`, no top-level `exec`/`eval`/`globals`/`vars`, no bare module reference) — the refined numbers below are never diluted by the coarse fallback path.

Distribution over all 740 fastapi/ top-level statements, fraction = share of 578 test files whose closure includes the statement:

| Rule | median | mean | share >50% | share <10% | share =0% |
|---|---|---|---|---|---|
| File-level (current) | 53.98% | 50.12% | 91.4% | 8.6% | 0.9% |
| Name-level (this refinement) | 0.17% | 14.51% | 11.8% | 70.1% | 46.9% |

Specific asks:
- **New function added to `fastapi/routing.py`**: file-level selects the 54.0% of test files that reach `fastapi.routing` transitively (312/578) regardless of what the new function does. Name-level selects ~0% by construction (nothing references a name that doesn't exist yet) — confirmed generally by soundness case 7.
- **Constant edit, `fastapi._compat.shared.PYDANTIC_VERSION_MINOR_TUPLE`**: file-level 55.2% → name-level **0.2%**.
- **Pydantic model field edit, `fastapi/openapi/models.py`**: `Contact`/`Info` 54.5% → 34.6%; `Schema` 54.5% → 34.8% (openapi-schema-testing is inherently broad, so the win is smaller here — still real).

**Analysis time**: `fastapi/` alone parses+resolves in ~0.1–0.3s. Full tree (1102 modules, 9323 blocks): ~0.8–1.7s. Computing closures for all 578 test files: ~3.2–3.7s. Total pipeline for the whole fastapi measurement: under 5s.

**Important caveat on the static approximation itself** (not on the refinement): checking `fastapi.routing`'s hot-path dispatch functions (`get_request_handler`, `run_endpoint_function` — code that runs on essentially every request any `TestClient`-based test makes) gives name-level fan-in of **0.17%**, because no test file's *source text* ever names them; they're only reached by the real interpreter executing `APIRoute.__init__`'s stored callable at request time. A real `sys.monitoring` tracer would see these functions execute in nearly every test and correctly attribute them; the static "seed with everything the file references" approximation used here for part 3 cannot see execution, only text, so **the reported name-level numbers are a lower bound relative to what the actual tracer-fed system would select for hot-path code**, while being accurate for module-level constants/classes a test does or doesn't reference explicitly (like the `_compat` constant and pydantic models above). This is exactly why the real design (`testmon-plan.md`) feeds the def-block resolution from actual traced execution (rule 1) rather than static analysis — the static version here is a research instrument for measuring selectivity, not something to ship as-is.

## 4. Precision on `oss/httpx`

31 test modules, 441 `httpx/` top-level statements.

| Rule | median | mean | share >50% | share <10% | share =0% |
|---|---|---|---|---|---|
| File-level | 96.77% | 89.58% | 92.1% | 7.9% | 7.9% |
| Name-level | 0.00% | 17.92% | 17.9% | 69.8% | 63.3% |

File-level is worse here than on fastapi (median 96.8% vs 54.0%) because httpx's small test suite imports `httpx` broadly and its `__init__.py` re-export surface pulls in nearly everything. The `httpx` package itself triggered the bare-module-reference fallback (`tests/test_exported_members.py` and `tests/test_exceptions.py` pass the `httpx` module object around, e.g. `dir(httpx)`/`getattr(httpx, ...)`), which is exactly the scenario the fallback rule exists for — and is a genuine, not hypothetical, occurrence. Analysis time: negligible (~0.1–0.2s build, ~0.03–0.05s measurement).

## Honest assessment

**Is the name-level refinement sound enough to replace the file-level module block?** Not as specified, without two amendments this prototype needed to add:

1. **Decorator-registry patterns are unsound without one-hop interprocedural widening.** A bare "fold an effect into whatever first-party name its own direct references touch" rule misses `@register("key") def handler` populating a module-global dict through a closure, because the decorator's direct reference is to the factory, not the dict. This is a common pattern (fastAPI-style routers, plugin registries, SQLAlchemy's declarative registry) and the fix (walk one level into the factory's body for its own mutations) is cheap but not something the original proposal called out — it needs to be part of the design, not an afterthought.
2. **Whole-module fallback must expand through the fallback module's own re-exports**, not just its literal statements, or the fallback (meant to be conservative) can under-select. Found and fixed as a real bug during this work.

With those in place, every soundness case behaved correctly except the two everyone already expects to be gaps (unrelated re-export-hub edits are *correctly* excluded; that's the point, not a defect).

**What it buys in precision:** substantial and specific, not uniform. Module-level constants/aliases/classes that a test either imports-and-uses or doesn't (the `_compat` constant: 55%→0.2%; a brand-new function: 54%→0%) get essentially exact treatment. Broad structural hubs like `openapi/models.py` (used pervasively for schema assertions) shrink less (54.5%→~35%) because the fan-in there is real, not an artifact of coarse blocking. The median fastapi statement drops from being reached by 54% of the suite to 0.17% — but see the caveat above: that number is a static-analysis lower bound, and the true tracer-fed number for hot-path internals would sit much higher (correctly), while cold/peripheral code would stay near the refined-rule's low numbers.

**Fallback frequency**: zero trigger rate across all of fastapi + its 578 tests + docs_src (1102 modules). One trigger in httpx's much smaller tree (bare `httpx` module reference in 2 test files). This suggests the fallback is a real safety net but rare in well-structured library code — it earns its keep on test code that does reflective things (`dir(module)`, `getattr(module, name)`), not on production code.

**Remaining gaps worth carrying into a real implementation**, roughly by how often they'd bite:
- Dynamic module loading (`importlib.import_module` with a runtime-computed name) is not a corner case — it's 27% of fastapi's own test suite (156/578 files), all through parametrized-fixture tutorial loading. Already the documented fallback in `testmon-plan.md` ("new/removed file → full run"); this measurement confirms it's necessary and shows real scale.
- Function-scoped imports never referenced elsewhere, and imports whose bound name is never referenced anywhere, are invisible — worth an explicit statement in the design that "import for side effect only" needs its own detection (e.g. treat any first-party import target as always contributing its module's global effects, regardless of whether the bound name is later used).
- SQLAlchemy-style `ForeignKey("table_name.column")` is unreachable by any identifier-based heuristic since table names and class names aren't textually related; would need a `__tablename__`-keyed side index specifically for ORM base classes, which is framework-specific and probably out of scope for a general analyzer.
- One-hop limits everywhere (decorator-factory widening, re-exported-instance attribute chains) mean two levels of indirection lose precision silently (fall through to "not resolved" rather than a flagged fallback) — worth deciding whether that should instead trip the whole-module fallback so it fails safe rather than fails silent.
