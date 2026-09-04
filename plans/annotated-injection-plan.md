# `Annotated` injection

Companion to `dependency-typing-plan.md`, which covers the type information this spelling carries.

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

The fourth reason is the one that matters most in practice, and it is measured in
`dependency-typing-plan.md`: `db=Depends(db_fx)` leaves `db` unannotated, and under mypy that
parameter is `Any` — the test body stops being checked against it, quietly. `Annotated` has no
default to infer from, so the type is always written down.

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

## What landed — Phase 1

The runtime reads both spellings. `plan_of` is one walk over
`co_varnames[:co_argcount + co_kwonlyargcount]`, taking each parameter's injection from its
default or from `_annotated_injections`; `_reject_annotated_depends` and its `TypeError` are gone.

**The 3.14 spike settled on `STRING`**, as the plan guessed, and the measurements are worth
keeping. In a module with `from __future__ import annotations`, every format — `VALUE`,
`FORWARDREF`, `STRING` — hands back the raw source text, so nothing is ever evaluated there.
Without the future import, `func.__annotations__` and `VALUE` raise `NameError` for a
`TYPE_CHECKING`-only name (which is the regression to avoid), `FORWARDREF` returns real objects
with a `ForwardRef` in place of the unresolvable half, and `STRING` returns source text.
`FORWARDREF` would have kept the cheap object path, but it evaluates the type half to get there,
which is exactly what the parse-don't-evaluate policy is for. So: `STRING` on 3.14+,
`__annotations__` on 3.13, and on 3.14 the whole thing is skipped when `__annotate__ is None`,
which is the cheap bail-out for a function with no annotations at all.

Five things the plan did not anticipate:

- **A PEP 695 alias is a `TypeAliasType`, not an `Annotated`**, so both paths unwrap `__value__`
  before looking for metadata. Bounded rather than a `while`: `type A = A` hands back the alias
  object itself, forever.
- **An alias, and the fixture in `Depends(...)`, have to be module-level.** The plan documents
  this for the fixture under PEP 563; on 3.14 every annotation is source text, so it is the rule
  everywhere and the reference says so plainly. Where the marker is found but its call cannot be
  evaluated, that is a `DIError` naming the parameter rather than a silently dropped injection.
- **A marker held in a module-level name** — `MARKER = Depends(db)`, `Annotated[int, MARKER]` —
  resolves by the same dictionary lookup the alias case uses, so it costs nothing extra to accept.
  Same for `Annotated` itself being imported only under `TYPE_CHECKING`: unresolvable, so the
  spelling is enough to recognize the subscript, and the marker inside it still has to resolve to
  velox's own `Depends` by identity before anything is evaluated.
- **`typing` flattens `Annotated[Annotated[X, a], b]`** into one object carrying both markers, so
  the source path recurses into a nested subscript to see what the object path is handed for free.
- **On 3.14, a hand-mutated `__annotations__` dict is invisible**, since `STRING` recomputes from
  `__annotate__`. This only matters for the not-a-parameter diagnostic, which is reachable through
  `functools.wraps` on both versions — 3.13 copies `__annotations__`, 3.14 copies `__annotate__`.
  The wraps case that must *not* raise is the `(*args, **kwargs)` wrapper, guarded by
  `CO_VARARGS | CO_VARKEYWORDS` rather than by the annotation itself.

The pre-parse gate is the annotation's *shape*, not the plan's substring test on `Depends` and
`Annotated`: both names arrive under whatever alias the user imported them as, and rejecting on
spelling would have dropped `x: Ann[int, dep(db)]` silently. A whole annotation that is a dotted
name is an alias and is resolved by lookup with no parse at all — which is most annotations —
and one with no subscript in it has nowhere to hold metadata. The rest are parsed.

Collection cost, per `plan_of` call — once per fixture at decoration, once per test at collection.
On a five-parameter function whose annotations are objects: 2.71 µs before this change, 3.24 µs
after. The same function in a module that stringifies its annotations, one parameter injected:
20 µs, which is where the two `ast.parse` calls and the one `eval` land.

`tests/di/test_typing.py` pins that a checker sees `Session` at an annotated site and through an
alias. CI runs the suite under 3.13 and 3.14, since the two take different halves of this code.

Phase 3's sweep is untouched: `docs/reference/fixtures.md` gains the section describing both
spellings and the module-level rule, and `rationale.md` the parse-don't-evaluate policy, but
README, guide, and `examples/` still show the default position throughout.

## Phase 2 — codegen (`velox-migrate`)

- `convert/wiring.py`: `_param`/`_inject` emit `cst.Annotation` wrapping the original annotation in
  `Annotated[...]` with a `Depends(...)` metadata element, instead of a `default=`. Add the
  `typing.Annotated` import alongside `velox.Depends`.
- `_rewrite`'s reordering branch becomes reachable only for the `request` → `param` rewrite, since
  nothing gains a default any more. Confirm and then delete what is dead; the module docstring's
  "two constraints" paragraph loses one of its constraints.
- **An unannotated source parameter gets `Annotated[Any, Depends(fx)]`.** No `--syntax` flag, no
  fallback to default position. The point is that it does not paper over the missing type: under
  the old spelling mypy silently gave that parameter `Any` anyway, and `Any` written down is the
  same amount of type information said out loud. Where the type *is* recoverable from the
  fixture's own return annotation, `dependency-typing-plan.md` recovers it; where it isn't, the
  conversion report names the site.
- **The recovery itself has already landed**, in `convert/annotate.py`, writing into default
  position: `db: Session = Depends(db_fx)`. `infer` (the rule table over the annotation's source
  text) and `Resolver` (importability, aliasing, the degraded worklist) are both independent of
  the spelling; this phase changes only the emission in `wiring.py`. Two things do change with it.
  The not-recoverable case, which today writes *no* annotation because a default is there for
  pyright and pyrefly to infer from, becomes `Any` once the default is gone — the plan's letter,
  restored on the premise that makes it true. And the `from __future__ import annotations` that
  the default-position form must write, because such an annotation is evaluated when the `def` is
  read, stops being load-bearing for the `TYPE_CHECKING` imports, since the parse-don't-evaluate
  policy covers them; whether to keep writing it anyway is a decision this phase should make
  rather than inherit.
- `matrix.py`: re-derive which refusal rows survive. Candidates that should shrink or go are the
  ones about binding position and about a signature velox cannot reorder. `docs/migrate/matrix.md`
  is generated from it.
- Golden output across `velox-migrate/tests/test_convert*.py` moves in a large mechanical diff.
  Re-run the httpx2 conversion (`plans/httpx2-audit.md`) afterwards and diff the refusal counts —
  a drop there is the concrete win to report.

## What landed — Phase 2

`wiring.py` writes `Annotated[T, Depends(fx)]` and no default. `typing.Annotated` is imported at
run time beside `velox.Depends`, and `typing.Any` where the type could not be recovered — the
plan's letter, restored as it said, and `convert/annotate.py`'s docstring no longer argues against
it. Refusals, node ids, and the report are untouched; the corpus suites still convert, run under
velox, and re-convert to nothing.

**The future import stays, and stays load-bearing.** The premise for dropping it was that
parse-don't-evaluate covers a `TYPE_CHECKING`-only type, and it covers only what *velox* does with
the annotation. On 3.13 the annotation is still an expression Python evaluates when the `def` is
read, so a module naming a type it imports only under `TYPE_CHECKING` needs the future import as
much in metadata position as in default position. It is written on the same condition as before:
this pass claimed a `TYPE_CHECKING` import.

Three things the plan had backwards or did not anticipate:

- **The reordering branch is not dead; its trigger inverted.** The old one — a `params=` fixture's
  `request` becoming the bare `param` after an injected parameter that had a default — cannot
  happen now that nothing gains a default, and the corpus's `retries` keeps the order it was
  written in. What survives is the reverse: a parameter a body asked for by name is appended to
  the signature and, if the source gave some earlier parameter a default, must move ahead of it.
  So the split stays, as `_ordered`, with unit tests pinning the shapes the corpus does not
  produce. Two of those the split had no answer for: a defaulted positional-only parameter leaves
  a positional one without a default nowhere valid to go at either end, so the new parameter goes
  behind a `*` — velox binds by keyword, so that costs the signature nothing — and keyword-only
  parameters bind by name in whatever order they are written, so the split never applies to them.
- **Converting a converted tree wrapped its own annotation a second time.** The rewrite is driven
  by the pytest dump, so it re-injects a parameter that is already injected; `_bare` takes an
  `Annotated[T, Depends(...)]` back down to `T` before wrapping, which is what makes the second
  conversion a no-op again.
- **No matrix row refuses on binding position**, so none shrank — `VX001`'s description is the one
  row this changes. Worth noting separately: `wiring.py` refuses a positional-only injected
  parameter under `VX017`, which is a `request`-as-a-value row. That mislabel predates this work
  and the annotated form does not change the refusal, but a user reading the report is told the
  wrong thing.

**httpx2 re-run** (`httpx2-audit.md`), converted with each spelling in turn: the refusal counts
are identical — `VX324` 23, `VX216` 6, `VX030` 4, `VX112` 2, plus one `VX205` site — and both
converted trees collect 1466 tests, 1 skipped, 109 collection errors under velox, the errors being
the refused tests announcing themselves as designed. The hoped-for drop was never there to find,
because refusal never depended on binding position. The win the suite does show is in the
signatures: 57 injection sites, every one carrying a real type and none degraded to `Any`, and the
one multi-line signature the old form mangled — `test_load_ssl_config_cert_and_encrypted_key`,
whose parametrized `password` was hoisted ahead of two injected parameters that were then written
flush against the left margin — now keeps its order and its indentation.

`docs/migrate/index.md`'s before-and-after example and the generated `docs/migrate/matrix.md` show
what the tool writes now. The rest of Phase 3's sweep is untouched.

## Phase 3 — docs and examples

`Annotated` becomes the form shown everywhere; default position gets one titled section in the
fixtures reference explaining that it exists, is supported, and is shorter.

README.md · docs/index.md · docs/guide/index.md · docs/reference/fixtures.md ·
docs/reference/index.md · docs/reference/builtins.md · docs/migrate/index.md ·
docs/migrate/matrix.md (generated) · `velox/__init__.py`'s module docstring · all three suites
under `examples/` (~130 sites, largely mechanical) · a ROADMAP note while this is in flight.

## What landed — Phase 3

`Annotated` is now the form shown first everywhere the plan named: README.md, docs/index.md,
docs/guide/index.md, docs/reference/fixtures.md, docs/reference/builtins.md. The fixtures
reference gained the titled "The short form" subsection for default position, moving ruff's `B008`
note there since metadata isn't a default and never trips it. `docs/reference/index.md`,
`docs/migrate/index.md`, `docs/migrate/matrix.md` and `velox/__init__.py`'s module docstring
needed nothing: Phase 2 had already updated the first two, and the docstring never showed either
spelling to begin with.

All three `examples/` suites converted mechanically — 141 injection sites across 12 test/fixture
files, done with a one-off libcst pass (`Param.default` a `Depends(...)` call → wrapped into
`Annotated[...]`, default dropped) rather than by hand, then `ruff check --fix` and `ruff format`
for import sorting and line wrapping. Each suite's own `pyproject.toml` lost its
`extend-immutable-calls = ["velox.Depends"]` bugbear entry — no test takes a `Depends(...)`
default any more, so nothing trips `B008` — except 01-fastapi-crud, which keeps the entry for
`fastapi.Depends` alone: `app/main.py` is the application under test, not a velox suite, and its
routes still take FastAPI's own `Depends()` as a default, the idiom that file is demonstrating.
All three suites' tests and lint/format checks pass unchanged after the rewrite.

`app/main.py` was deliberately left alone for the same reason — nothing in this plan touches
FastAPI's own injection syntax, only velox's.

## Follow-up — alias shapes

FastAPI teaches the reusable injection as a plain assignment, `CommonsDep = Annotated[dict,
Depends(common_parameters)]`, not as the `type` statement Phase 1 wrote its tests against. That
shape already worked, in both paths and across modules: an assignment is an ordinary module
global, so the whole-annotation dotted-name lookup resolves it exactly as it resolves a
`TypeAliasType`, whether the consumer binds it with `from deps import DbDep` or reaches it as
`deps.DbDep`. It is pinned now rather than left to coincidence.

Two shapes did not work, and both are closed:

- **A subscripted generic alias** — `type Repo[T] = Annotated[T, Depends(repo_fx)]`, used as
  `Repo[Account]` — was found by neither path. `typing` does not substitute into an alias for
  either of them: `get_type_hints(..., include_extras=True)` hands back `Repo[int]` itself, so
  the metadata is reachable only by following `__origin__.__value__` deliberately. Which settles
  it as never having been a parse-versus-evaluate question. Both paths now follow the alias,
  unsubstituted, since substituting a type parameter cannot change what the metadata holds.
- **An alias in the type half of an `Annotated`** — `Annotated[Db, "documentation"]` — was found
  only where `typing` had flattened the two into one at construction, which it does for the
  assignment form and not for a `type` statement or a subscripted alias. Both paths walk the type
  half now: the source path resolves a name there, and the object path recurses into
  `get_args(...)[0]` rather than trusting the annotation to arrive flat.

**Evaluating the annotation instead was reconsidered here and rejected.** In a module that does
not stringify its annotations, the object path *is* what evaluation yields — it reads the same
object `get_type_hints` would build — so the switch buys nothing that the source path was not
already given for free, and costs the `TYPE_CHECKING`-only type that Phase 1's spike measured.

What remains out of reach is an alias a stringifying module imports only under `TYPE_CHECKING`:
the name does not exist at run time, and no approach can resolve it. The parameter is reported at
collection as one nothing can supply, which is the loud half of the sharp edge the reference
already documents for a fixture held in a local variable.

## Sequencing

Phase 1 has landed, spike included, so the design risk is spent. Phase 3 depends on Phase 2 only
for `docs/migrate/`. The `--syntax` question is settled — always `Annotated` — and everything in
`dependency-typing-plan.md` has landed, so Phase 2 inherits a working type inference and has only
to re-spell what it emits.

Per the `dev-workflow` skill this is worktree work: `git worktree add ../velox-wt-annotated -b
annotated-injection`, `just sync`, `just check`, `/code-review` before merge.

## Risks

- Two spellings is two of everything to explain. The mitigation is that the second one is one
  section, not a parallel guide.

The other two are spent: the 3.14 annotation-format question is answered above, and the collection
cost is measured there rather than guessed at.
