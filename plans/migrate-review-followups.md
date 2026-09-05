# velox-migrate: findings from the high-effort correctness review

Eight-angle `/code-review high` of the dependency-typing merge (`25b6561`), scoped to
`velox-migrate/` and read for data loss or incorrect conversion. Two confirmed correctness bugs —
a missing `from __future__ import annotations` on reused `TYPE_CHECKING` imports
([convert/wiring.py](../velox-migrate/velox_migrate/convert/wiring.py)) and an over-broad
`usefixtures` suppression that swallowed real suite-authored marks
([audit/wiring.py](../velox-migrate/velox_migrate/audit/wiring.py))— were fixed directly, each with
a regression test that fails on the pre-fix code. This document catalogues what the same review
surfaced but left unfixed: lower-confidence correctness gaps, and reuse/simplification/efficiency/
altitude cleanups that are quality issues rather than data-loss risks.

## Correctness — lower confidence, needs a closer look

**`_takes()` misses `request` forwarded into a plain helper, or under a customized
`python_functions`.**
[audit/sources.py](../velox-migrate/velox_migrate/audit/sources.py) `_takes()` only treats a
`request` parameter as pytest's own when the innermost declaring frame `injects` — a fixture
factory or a function whose own name starts with `test`. Two real misses follow: a helper function
that a test hands its `request` fixture into (`def _register_cleanup(request): ...`) is not
recognized, so a hazardous `request.addfinalizer`/`.node`/`.getfixturevalue` use inside it goes
unreported; and a suite with `python_functions = ["*_check"]` in its ini has none of its real test
functions recognized either, since none start with `test`. The narrowing itself is deliberate and
partly tested (`test_a_request_parameter_of_a_function_pytest_never_calls_is_not_the_fixture`), but
neither miss has a regression test, and the `python_functions` case isn't mentioned by VX302 in a
way that stops the hazard scan from running on the wrong assumption. Needs a decision: read
`ground_truth.items`' actual collected names instead of a `"test"` prefix check, and/or track
`request`-forwarding into a helper explicitly.

**`_absolute()`'s relative-import level arithmetic may be off by one at the rootdir boundary.**
[convert/annotate.py](../velox-migrate/velox_migrate/convert/annotate.py) resolves a relative
import's absolute dotted path from `node.level` and the file's parent parts:
`node.level - 1 > len(parts)` guards against going past the rootdir. For a file two packages deep
and a level-3 import, this evaluates `2 > 2 = False` — not rejected — which may or may not match
the level-2 case's boundary. Flagged with low confidence; needs concrete fixtures (a `level=3`
import from a two-deep file) run through `_absolute()` to confirm whether it over- or
under-resolves, since a wrong answer here silently drops a `TYPE_CHECKING` import an injected
parameter's annotation depends on.

## Reuse

**`CLEAN_EXIT_STATUSES` redefined instead of imported.**
[verify/runners.py](../velox-migrate/velox_migrate/verify/runners.py) redeclares
`CLEAN_EXIT_STATUSES = frozenset({0, 1, 5})` rather than importing the constant `schema.py` already
defines and `cli.py` already reuses. Unlike `extractor.py`'s deliberate self-contained copy (which
has to stay import-free to run standalone in a foreign environment), `verify/runners.py` has no
such constraint — it already imports from the package. A future change to which exit codes count
as "clean" would silently miss this copy.

**Hand-rolled Markdown tables instead of the existing `_table()` helper.**
[verify/report.py](../velox-migrate/velox_migrate/verify/report.py)'s `markdown()`/`_row()` build
pipe tables by hand instead of reusing `report/markdown.py`'s `_table()`/`_line`/`_cell`, which
already handle column padding and alignment for the audit report. The hand-rolled version has no
column-width padding and will drift in style from every other table velox-migrate emits, and won't
pick up future escaping fixes (e.g. for `|` in a test id).

**`annotate.bindings()` duplicates `layout.module_level_names()`'s node-matching.**
Both walk a module's top-level `ast` body collecting the same binding shapes
(`FunctionDef`/`ClassDef`/`Assign`/`AnnAssign`/`Import`/`ImportFrom`), kept separate because
`bindings()` additionally tracks each name's originating import and descends into
`TYPE_CHECKING` blocks. `Resolver._claimed()` unions both to avoid name collisions, so a future
case added to one (e.g. `ast.TypeAlias`, already only in `bindings()`) and not the other risks the
two disagreeing about what a module already binds.

**JSON-artifact writing reimplemented instead of reusing `report/payload.write()`'s pattern.**
`outcomes.py` and `verify/report.py::write_payload()` each hand-roll "dump JSON with `indent=1`,
append a trailing newline" instead of the one place that pattern is already factored
(`report/payload.py::write()`). `outcomes.py` has a standalone-plugin constraint that rules out
importing it directly, but `verify/report.py` doesn't — a shared writer would keep the convention
(indent width, `sort_keys`, …) in one place.

## Simplification

**`typed`/`site` threaded as optional parameters through 4+ nested functions.**
[convert/wiring.py](../velox-migrate/velox_migrate/convert/wiring.py)'s `typed: list[TypeImport] |
None` travels through `_rewrite` → `_inject`/`_param` → `_annotation` purely to reach one
`.extend()` call, with an `is not None` guard even though every real call site passes
`self.typed`. Similarly [convert/plan.py](../velox-migrate/velox_migrate/convert/plan.py) threads
`typed: Resolver | None` and `site: str` through `_work` → `_injections` → `_test_injections` →
`_from_names` to reach one `typed.of(fixture, consumer, site=site)` call. Cleaner as return values
bubbled up by the one method that owns the accumulator, rather than parameters threaded down.

**Duplicated exit-status/artifact error handling in `run_pytest` and `run_velox`.**
[verify/runners.py](../velox-migrate/velox_migrate/verify/runners.py) repeats the same
missing-artifact-then-bad-exit-code check in both functions, and they've already drifted — only
`run_pytest`'s failure branch unlinks the stale record. One shared `_require_clean(...)` helper
would keep them in sync.

**`Audit.type_readiness` defaults to a shared instance instead of `field(default_factory=...)`.**
[audit/findings.py](../velox-migrate/velox_migrate/audit/findings.py) line ~231:
`type_readiness: TypeReadiness = TypeReadiness()` constructs one object at class-definition time
and reuses it as every `Audit`'s default. Harmless today only because `TypeReadiness` is frozen and
all-immutable; the shape is the classic mutable-default bug, and any future mutable field added to
`TypeReadiness` would have every default-constructed `Audit` silently share state.

## Efficiency

**`Resolver._factory` re-parses a fixture's source file on every injection site.**
[convert/annotate.py](../velox-migrate/velox_migrate/convert/annotate.py) calls
`ast.parse(source)` plus an AST walk fresh on every `.of()` call, with no memoization keyed on
`(source, qualname)` — unlike the sibling `_table()` lookup, which is cached. A fixture injected
into hundreds of tests reparses its defining module hundreds of times per `convert` run.

**`readiness._injection_sites` recomputes dependency edges `Item.walk()` already computed.**
[audit/readiness.py](../velox-migrate/velox_migrate/audit/readiness.py) calls
`item.dependencies(fixture)` again for every fixture `Item.walk()` yields, even though `walk()`
already calls `dependencies()` internally to drive its own traversal. Doubles the graph-walk cost
of type-readiness assessment for large fixture graphs.

**`verify.run()` runs pytest and velox sequentially when neither depends on the other.**
[verify/__init__.py](../velox-migrate/velox_migrate/verify/__init__.py) `run()` executes
`run_pytest(...)` then `run_velox(...)` back to back, though `compare()` only needs both results at
the end and each is a blocking subprocess call. Running them concurrently (e.g. via
`ThreadPoolExecutor`, since each releases the GIL) would cut `verify`'s wall time to roughly the
slower of the two rather than their sum.

## Altitude

Several places encode one specific plugin's mechanism where a general-sounding name suggests a
general mechanism:

- `wiring.py`'s `_backend_findings`/`_backend_name` (VX324) key detection of "parametrized onto a
  non-asyncio backend" off exactly the `anyio_backend` callspec parameter and the literal string
  `"asyncio"` — a different async plugin exposing an equivalently-purposed fixture under another
  name produces no finding.
- `audit/sources.py`'s injection heuristic (`name.startswith("test")`, described above) never
  checks the enclosing class against `Test*`/`python_classes` either, so a plain helper class with
  a `test_*`-named diagnostic method is treated as a pytest test method.
- `audit/readiness.py`'s `_SYNTHETIC_PREFIXES = ("_xunit_", "_unittest_")` duplicates
  `audit/wiring.py`'s own constant of the same name rather than sharing it, and both are a
  hardcoded two-entry tuple that a future pytest/plugin internal-naming change could silently miss.

None of these are urgent — each degrades an audit finding's precision, not conversion output — but
they're the kind of thing worth revisiting before adding a second async-plugin or second
xdist-shaped plugin to the support matrix.

---

See [ROADMAP.md](../ROADMAP.md) § Code quality consolidation.
