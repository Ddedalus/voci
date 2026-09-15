# Global decisions

Decisions that shape the whole system, not any one module. See [rationale.md](../rationale.md) for
the index, and the per-module files alongside this one for decisions local to a single file.

## The shape of the system

```
argv + [tool.voci]  →  discovery  →  collection  →  DI plan  →  execution  →  reporting
```

One pass, one direction, no node tree. Discovery is a directory walk and a name filter. Collection
imports each test file and produces a flat `list[TestRecord]`. Each record gets a static resolution
plan for its entire transitive fixture graph. Execution dispatches those records as concurrent
tasks on one event loop. Reporting turns the results back into logical order.

## Everything per-test lives in a ContextVar

The runner has no module-global mutable state. Anything that varies per test is a `ContextVar`; anything genuinely shared is owned by an explicit object passed down.

**Never reach for a module-level mutable**.

## One process, one event loop

Tests run as concurrent `asyncio` tasks in a single process. This project targets I/O-bound suites typical of web apps.

## Fixtures are imported, not resolved by name

pytest finds a fixture by matching a parameter name against everything visible through the
`conftest.py` chain. voci requires you to import the function and name it in a `Depends()`
default, exactly as a FastAPI route does.

What this buys: "go to definition" works, renames are safe, unused fixtures are visibly unused, and
a typo is an `ImportError` at collection instead of a fixture-not-found at run time. What it costs:
you write the import.

The injection plan is read from `__code__`/`__defaults__` directly — never `inspect.signature`, and
never by unwrapping `__wrapped__`. That keeps collection fast and predictable, at the price of one
known sharp edge: a decorator that replaces a test's signature with `(*args, **kwargs)` hides its
`Depends()` defaults from collection entirely.

## Handling of annotations
A `Depends()` in a parameter's `Annotated[...]` metadata is read too, which is the one thing an
annotation is load-bearing for. Annotations are not reliably objects — `from __future__ import
annotations` makes every one of them a string, and on 3.14 they are computed on demand and raise
for a name that exists only under `TYPE_CHECKING` — so voci **parses rather than evaluates**
(`_di/fixtures.py`): it reads the annotation's source text with `ast` and evaluates only the
metadata elements that are calls to voci's own `Depends`. Names are otherwise resolved by
dictionary lookup in the module's globals, never `eval`, which is what lets an alias
(`type Db = Annotated[Session, Depends(db_fx)]`) carry a marker.

The type half is never touched. That is deliberately more permissive than FastAPI, which requires
every annotation on an injected callable to resolve at run time: wherever the module itself
doesn't evaluate its annotations, voci injects against a type imported under `if TYPE_CHECKING:`
and collects a test whose other parameters are annotated with names that resolve to nothing at
all. What it costs is that the *marker* still has to be evaluable, so the fixture it names has to
live in the module's globals rather than in a local variable — a `DIError` at collection when it
doesn't.

The line is drawn at name-based resolution, not at where a dependency is declared. A container —
a test module, or a package `__init__.py` covering that directory and below — can declare fixtures
on behalf of every test inside it, with `voci.use(...)`:

```python
voci.use(db_reset)
```

The fixture is still an imported object, named at a site you can jump to; the declaration lives on
the container rather than in each signature, and its value is discarded. FastAPI draws the same
line with `APIRouter(dependencies=[Depends(...)])`. What a reader gives up is that a test's
dependencies are readable from its signature plus the headers of the files above it rather than
from its signature alone; what the DI system keeps is everything the static half rests on — no
lookup that can fail at run time, no shadowing rule, and a `ResolutionPlan` that still describes
the whole graph, since a declared fixture becomes a step like any other, just one nothing in
`root_args` points at.

Declarations accumulate rather than override. A package's apply ahead of those of the modules
under it, and there is no proximity rule by which one can replace another — that rule is the
machinery that makes a conftest chain hard to read, and without it, finding what reaches a test is
reading each `__init__.py` on the way down, in order. A test that should not have a fixture goes
where nothing declares it.

## Logical order governs all output

Collection order and completion order are entirely separate. Failure details, the
short summary, and every machine-readable emitter are sorted back into logical order.

## One aggregated result per test

setup, call and teardown are internal phases. Exactly one `TestResult` reaches the reporter, and it is JSON-round-trippable from the first commit. pytest emits three reports per test and every
consumer downstream re-aggregates them.

Serializable results are not speculative future-proofing — they are what makes a per-test
subprocess tier, `--report-json`, and any eventual multi-process mode possible without touching the
reporter, and they make the runner testable without a terminal.

## No plugin system

There are no hooks and no entry-point scanning. Extension happens through dependency injection: a
fixture is the unit of composition, and it is an ordinary function you can import, wrap, or replace.

## Assertion introspection is vendored, not reimplemented

voci vendors pytest's assertion rewriter rather than writing one. Assertion introspection is
mature, subtle, and the single feature users would most notice missing; there is nothing to gain
from a second implementation.

## Escalate, never silently degrade

Where a mechanism can't be made concurrency-safe — raw global patching, file-descriptor capture,
per-test warning filters — voci escalates the affected test to a stricter tier or fails loudly and
names the cause. It never quietly does the unsafe thing.
