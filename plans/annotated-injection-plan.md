# `Annotated` injection

Support `db: Annotated[Session, Depends(db_fx)]` alongside today's `db: Session = Depends(db_fx)`,
and make the annotated form the one velox teaches and generates.

## Why

`Depends()` is typed `-> T` and returns a `Dependency` sentinel. The lie is deliberate and it is
what makes `db: Session = Depends(db_fx)` type-check, but it is also what a careful reader notices
first: the default is not a value the parameter could ever hold, calling the test by hand hands it
a sentinel, and a parameter with no default can never follow an injected one. FastAPI hit all
three and moved its own docs to `Annotated`, which fixes them without a cast: the marker sits in
metadata, where an arbitrary object belongs, the parameter keeps its real annotation, and an
injected parameter has no default at all.

The last point is not only cosmetic. Today `wiring.py` reorders any converted signature that mixes
injected names with parametrized ones, because a parameter without a default cannot follow one
that has it. Under the annotated form, nothing gains a default and no signature has to move.

## Decided

- **One spelling, `Depends`.** The same exported callable works in metadata and in default
  position, byte-identical to FastAPI, so `type Db = Annotated[Session, Depends(db_fx)]` aliases
  work and there is one name to document. `Depends` keeps its `-> T` signature for the sake of the
  default-position form; in metadata the declared return type is irrelevant.
- **velox never evaluates the annotated type**, only the marker. See below.
- **Both forms stay supported.** Default position is not deprecated; it stays the short form.
- **`Annotated` is canonical** in README, `docs/`, and `examples/`, and becomes velox-migrate's
  generated style.

## The load-bearing change

`_di/fixtures.py` opens with the claim that annotations are never load-bearing — the plan is read
from `__code__`/`__defaults__`, never `inspect.signature`, never `get_type_hints`. That invariant
ends here, and the cost has to be paid deliberately, because annotations are not always objects:
under `from __future__ import annotations` (PEP 563) every annotation in the module is a *string*,
so `Depends(db_fx)` is never called and there is nothing for velox to find unless it turns that
string back into an object. On 3.14 (PEP 649) annotations are lazy and evaluating them raises for
any name that isn't importable at run time.

**Policy: parse, don't evaluate — except the marker.** For a string annotation, velox parses it
with `ast` (no execution), locates the `Annotated[...]` metadata elements that are calls to
velox's own `Depends`, and evaluates only those call expressions in the function's globals. The
type half of the annotation is never touched, so a `TYPE_CHECKING`-only type is fine, and a
function whose *other* parameters carry unresolvable annotations still collects. This is strictly
more permissive than FastAPI, which requires every annotation on an injected callable to resolve
at run time.

Its one new sharp edge, to be documented: under PEP 563 a string annotation cannot see local
names, so the fixture object named in `Depends(...)` must be reachable from module globals. A
fixture held in a closure variable works only in a module without `from __future__ import
annotations`.

## Phase 1 — runtime (`velox/_di/fixtures.py`)

### `plan_of`, rewritten as one signature-order walk

Today: positional defaults, then `__kwdefaults__`. New: walk
`code.co_varnames[:co_argcount + co_kwonlyargcount]` once, in order, and for each parameter take
its injection from whichever source declares one — a `Dependency` default, or a `Dependency` in
its annotation's metadata. This is order-preserving for every existing suite (positional-defaulted
parameters in order, then keyword-only in definition order is exactly what the two loops produce
today), so no `PlanStep` ordering, cache key, or report changes for code that doesn't adopt the
new form.

Rules the walk enforces:

- A parameter declared **both** ways is a `DIError` naming it, regardless of whether the two name
  the same fixture. Ambiguity, not a merge.
- Two `Dependency` objects in **one** annotation's metadata is a `DIError`.
- The existing positional-only rejection applies to both spellings, unchanged: velox binds by
  keyword.
- Only names that are actual parameters of `func.__code__` count. `functools.wraps` copies
  `__annotations__` (it does not copy `__defaults__`), so a wrapper presenting `(*args, **kwargs)`
  would otherwise appear to carry injections it has no parameters for. Guarding by `co_varnames`
  keeps the existing, documented sharp edge — a signature-replacing decorator hides injections —
  rather than trading it for a new false positive.

### `_annotated_injections(func)`, new

1. Read the annotation map. On 3.14+ prefer `annotationlib.get_annotations(func, format=STRING)`
   so every entry arrives as source text uniformly and nothing is evaluated eagerly; on 3.13 read
   `func.__annotations__`, whose values are objects or strings depending on the module.
   *Spike this first* — confirm what `STRING`/`FORWARDREF` actually return for
   `Annotated[X, Depends(f)]` on 3.14 before committing to the shape.
2. **Object path**: `get_origin(a) is Annotated` → scan `get_args(a)[1:]` for `Dependency`
   instances. No parsing, no eval.
3. **String path**: fast-reject any text that neither contains `Depends` nor is a bare
   dotted name (the alias case), then `ast.parse(text, mode="eval")`.
   - A bare `Name`/`Attribute` is an alias (`db: Db`): resolve it by dictionary lookup in
     `func.__globals__` and `getattr` from there — never `eval` — and re-enter the object path
     with the result. A module-level `type Db = Annotated[...]` is a real object, so this is the
     whole alias story.
   - Otherwise find the `Annotated[...]` subscript, and for each metadata element that is a
     `Call` whose callee *name* resolves (same lookup, no eval) to `velox.Depends`, compile and
     evaluate that single node in `func.__globals__`. A metadata element that is not such a call
     is left entirely alone: velox must not execute arbitrary annotation code.
4. Cost: one `ast.parse` per parameter whose annotation text mentions `Depends`, once per fixture
   at decoration and once per test at collection. Nothing for a suite that doesn't use the form,
   and nothing for the object path.

### Everything downstream

- `_check_missing_injections` — an annotated injection has no default, so it lands in
  `required_positional`; it is already excluded via `injected`, so the check works as written.
  Re-read it against annotated injections landing *before* non-injected parameters, which the
  default-position form could never produce.
- Delete `_reject_annotated_depends` and its `TypeError`. Keep a diagnostic for the one case that
  is still silent: a `Dependency` found in metadata for a name that is not a parameter.
- `Fixture.__init__` — no change; it calls `plan_of`.
- `velox/_collection/collect.py:461` and `velox/_di/runtime.py:272` — comments referencing the
  rejected-`Annotated` path.
- Module docstring of `_di/fixtures.py` and the "Fixtures are imported, not resolved by name"
  section of `rationale.md` — both state the never-read-annotations invariant. `rationale.md`
  gains the parse-don't-evaluate policy and why velox is deliberately more permissive than
  FastAPI here.

### Tests

`tests/di/test_fixtures.py` gains a section mirroring the existing default-position cases:
annotated injection on a test and on a fixture; alias via `type X = Annotated[...]`; module with
and without `from __future__ import annotations`; `TYPE_CHECKING`-only type; both-spellings
conflict; two markers in one annotation; positional-only rejection; annotated injection before a
required parameter; interaction with `@velox.parametrize` `known_params`, with
`velox.use(...)`, with `@mock.patch`'s leading positionals, and with a `params=` fixture's `param`.
A `functools.wraps` wrapper must still read as no injections. Plus a typing check that pyrefly
accepts the annotated form with no `cast` at the call site.

## Phase 2 — codegen (`velox-migrate`)

- `convert/wiring.py`: `_param`/`_inject` emit `cst.Annotation` wrapping the original annotation in
  `Annotated[...]` with a `Depends(...)` metadata element, instead of a `default=`. Add the
  `typing.Annotated` import alongside `velox.Depends`.
- `_rewrite`'s reordering branch becomes reachable only for the `request` → `param` rewrite, since
  nothing gains a default any more. Confirm and then delete what is dead; the module docstring's
  "two constraints" paragraph loses one of its constraints.
- **Open decision — an unannotated source parameter.** Most pytest fixtures are unannotated, and
  `db: Annotated[Any, Depends(db_fx)]` is noise where `db=Depends(db_fx)` was not. Recommendation:
  a `--syntax {auto,annotated,default}` option defaulting to `auto` — annotated when the source
  parameter carries an annotation to preserve, default position when there is nothing to wrap.
  Needs sign-off; `annotated` unconditionally is the alternative.
- `matrix.py`: re-derive which refusal rows survive. Candidates that should shrink or go are the
  ones about binding position and about a signature velox cannot reorder. `docs/migrate/matrix.md`
  is generated from it.
- Golden output across `velox-migrate/tests/test_convert*.py` moves in a large mechanical diff.
  Re-run the httpx2 conversion (`plans/httpx2-audit.md`) afterwards and diff the refusal counts —
  a drop there is the concrete win to report.

## Phase 3 — docs and examples

`Annotated` becomes the form shown everywhere; default position gets one titled section in the
fixtures reference explaining that it exists, is supported, and is shorter.

README.md · docs/index.md · docs/guide/index.md · docs/reference/fixtures.md ·
docs/reference/index.md · docs/reference/builtins.md · docs/migrate/index.md ·
docs/migrate/matrix.md (generated) · `velox/__init__.py`'s module docstring · all three suites
under `examples/` (~130 sites, largely mechanical) · a ROADMAP note while this is in flight.

## Sequencing

Phase 1 alone is shippable and is the only phase with design risk. Phase 2 depends on it. Phase 3
depends on Phase 2 only for `docs/migrate/`. Suggested order: spike the 3.14 annotation-format
question, then Phase 1 behind its own tests, then Phase 2 with the `--syntax` decision settled,
then the docs sweep.

Per the `dev-workflow` skill this is worktree work: `git worktree add ../velox-wt-annotated -b
annotated-injection`, `just sync`, `just check`, `/code-review` before merge.

## Risks

- The 3.14 annotation-format spike is the one unknown that can change the shape of
  `_annotated_injections`. Do it first.
- Collection cost must stay at zero for suites that don't use the form. The substring fast-reject
  is what guarantees that; benchmark against `mod bench` if collection time moves.
- Two spellings is two of everything to explain. The mitigation is that the second one is one
  section, not a parallel guide.
