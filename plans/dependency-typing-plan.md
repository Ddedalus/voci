# Typing of injected dependencies

Prerequisite work for `annotated-injection-plan.md`. That plan changes the *spelling* of an
injection; this one is about the type information the spelling carries, on both sides of the
migration.

## The shortcoming, measured

`Depends(fx)` is typed `-> T` off `Fixture[T]`, and `@velox.fixture()`'s overloads already unwrap
the yielded/awaited type, so the *expression* has the right type. What a checker cannot do is give
that type to the parameter: `db` is a function argument, and its type comes from its annotation.
With no annotation there is only whatever the checker chooses to infer from the default — and the
three checkers disagree.

Probe: a `Session`-typed fixture, injected three ways, with two deliberate errors in each body
(`bad: str = db` and `db.nonexistent()`).

| Spelling | mypy 2.3.1 | pyright 1.1.411 | pyrefly (repo pin) |
| --- | --- | --- | --- |
| `db=Depends(db_fx)` | `db` is `Any`; **both errors missed**. `--strict` adds `no-untyped-def` on the function, but says nothing about the body. | infers `Session`; both errors caught | infers `Session \| Unknown`; both errors caught |
| `db: Session = Depends(db_fx)` | both caught | both caught | both caught |
| `db: Annotated[Session, Depends(db_fx)]` | both caught | both caught | both caught |

(mypy was run against a self-contained reproduction of the pattern: mypy 2.3.1 could not parse
velox's own sources, which is worth a separate look.)

So the failure is quiet and checker-dependent: a mypy user who writes the short form loses body
type-checking for that parameter and is told nothing. This is the same reason FastAPI pushes
`Annotated`, and it is the argument for velox teaching it too.

The annotated form has one further property that matters for codegen: **there is no default to
infer from**, so a missing type cannot hide. `Annotated[Any, Depends(fx)]` says "no type known"
out loud, where `db=Depends(fx)` said it silently. That is why the migration tool should write
`Any` rather than dress it up — decided, see "Changes to `annotated-injection-plan.md`" below.

## Part 1 — velox's own typing surface

An audit pass, small, no design risk. Everything a user's annotation could be checked *against*
has to be right first.

- Built-in fixtures already carry precise return annotations (`tmp_path() -> Path`,
  `capture() -> Capture`, `log_records() -> LogRecords`, `test_info() -> TestInfo`,
  `tmp_path_factory() -> TmpPathFactory`) and `builtin_fixture` keeps `func` for exactly that.
  Confirm each one under all three checkers, including `velox.fastapi.client`.
- Confirm the `FixtureDecorator` overload order does the right thing for a sync generator, an
  async generator, a coroutine, a plain callable, and the awkward pair — a fixture returning an
  iterator *as its value* (`-> Iterator[X]` meaning "the value is an iterator", not "this yields
  an X"), which the overloads cannot distinguish. Document the workaround if there isn't a fix.
- **`params=` is untyped end to end.** `params: Sequence[object]`, and the `param` parameter the
  fixture body receives is annotated by the user with nothing checking the two agree. Investigate
  whether a `params: Sequence[P]` → `param: P` relationship is expressible through the decorator
  protocol; if it isn't, say so in the reference rather than leaving it to be discovered.
- Decide whether `Dependency` should be exported for users who want to write their own
  `Annotated` aliases with a helper. Default answer: no, the alias needs no velox type.
- Reference docs gain a short, honest section: the table above, the recommendation
  (`Annotated`), and the note that the short form is fine but mypy will not check the body.

## Part 2 — prefactor before converting

A pytest suite whose fixtures have no return annotations cannot be converted into a typed velox
suite, whatever the codegen does. The tool's job is to *say so, specifically*, before the user
converts — not to invent types and not to hide the loss.

**Extend `velox-migrate audit`**, which already classifies what migrating costs, with a
type-readiness section: every fixture whose function has no return annotation, with its
`file:lineno`, ordered by how many injections would degrade to `Any` if it stays that way. That
list is a worklist a user can work through in their pytest suite, with pytest still green, before
converting anything.

`docs/migrate/index.md` gains a "prefactor first" step ahead of `convert`: annotate fixture return
types, run your checker, then convert. This is also the honest place to say that the conversion
preserves what type information exists and fabricates none.

## Part 3 — infer the trivial cases

When the fixture function *does* have a return annotation, the injected parameter's type follows
by inspection, and the converter should write it.

Rules, applied to the annotation's source text (never to an evaluated object):

- `-> X` → `X`
- `-> Iterator[X]`, `-> Generator[X, ...]`, `-> AsyncIterator[X]`, `-> AsyncGenerator[X, ...]` →
  `X`, matching `FixtureDecorator`'s own unwrapping
- `-> Awaitable[X]`, `async def ... -> X` → `X`
- anything else, including no annotation and `-> Any` → `Any`

Two mechanics this needs:

1. **The extractor must carry the annotation.** `model.FixtureDef` has no field for it; add one
   (annotation source text, plus the defining module) and bump `extractor_version`. Taking it
   from the live function at extract time is the only path that works for a fixture defined in a
   `conftest.py` the converter never rewrites.
2. **The name must be importable in the test's module.** The type is written in the fixture's
   module and the test may be elsewhere. Emit `if TYPE_CHECKING: from <module> import <name>`.
   This is safe precisely because of `annotated-injection-plan.md`'s resolution policy: velox
   parses the annotation and evaluates only the `Depends(...)` marker, so the type name never has
   to exist at run time. No new run-time imports, no import cycles. A name that cannot be
   attributed to an importable module falls back to `Any`.

Anything falling back to `Any` gets a row in the conversion report, naming the fixture and the
sites that lost their type — the same worklist as Part 2, now measured against the converted
suite.

## Changes to `annotated-injection-plan.md`

Its Phase 2 "open decision" about unannotated source parameters is settled: **no `--syntax` flag,
always `Annotated`, `Any` where nothing is known.** The default-position form stays supported by
the runtime and stays documented as the short form; it just stops being what the tool writes.

## Sequencing

Part 1 is independent of everything and can land now. Part 2 is independent of the annotated
spelling too — it only reads a pytest suite — and is the most useful thing to ship early, since a
user's prefactor work happens before conversion anyway. Part 3 lands with Phase 2 of
`annotated-injection-plan.md`, since it writes the annotations that plan's codegen emits.
