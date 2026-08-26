# marshmallow, migrated

Internal working document: the record of Phase 4 step 2 of
[migration-tool-plan.md](migration-tool-plan.md), where the tool was first run end to end against
a suite it was not built against. marshmallow was picked as the smoke test in
[oss-refactors-plan.md](oss-refactors-plan.md) precisely because it is clean — "if marshmallow's
audit comes back non-mechanical, the tool has a bug."

The audit did come back mechanical. The conversion did not.

## What was run

`oss/marshmallow` at `c7b559a`, green under pytest at 1188 tests — not the 652 the selection
document extrapolated, which was a count of test *functions* rather than of collected cases.

**Copy the suite out of `oss/` first.** `convert --write` rewrites the tree in place, and `oss/`
holds read-only submodules pinned at a commit. Running the conversion there would dirty the
submodule for every other purpose it serves.

The suite's
own `uv run pytest` will not do — that resolves marshmallow's `uv.lock`, which knows nothing about
velox.

```bash
cp -r oss/marshmallow /tmp/mm && rm -rf /tmp/mm/.git /tmp/mm/.venv
cd /tmp/mm
uv venv --python 3.13
uv pip install -e . -e ~/velox -e ~/velox/velox-migrate pytest simplejson

uv run pytest -q                       # the before: 1188 passed
uv run velox-migrate extract           # -> .velox-migrate/ground-truth.json
uv run velox-migrate audit             # -> migration-report.md, findings.json
uv run velox-migrate convert --write   # rewrites the tree
uv run velox --serial                  # the after: 1183 passed
uv run velox                           # same 1183, at concurrency
```

`~/velox` is wherever this repo is checked out; the two `-e` paths are what make the `velox` and
`velox-migrate` commands resolve inside that environment.

`convert --write` writes the whole tree before it prints a line of the plan or the diff, so
piping it into `head` or quitting the pager early leaves the converted suite intact. Redirect to a
file if the diff is too long to read inline.

## Result

| Runner | Passed | Failed | Wall |
|---|---:|---:|---:|
| pytest | 1188 | 0 | 0.77s |
| `velox --serial` | 1183 | 5 | 2.12s |
| `velox` (11.4x) | 1183 | 5 | 0.69s |

Nothing refused, no `VELOX-TODO` markers written, and concurrency cost nothing: the same five
tests fail serially and in parallel, so none of the 203 tests the audit flagged under VX412
(seeded randomness) actually depends on running alone.

## What the bug hunt found

Four defects in what the suite itself needed, and seven more behind them once the machinery had a
class-shaped override node to work with. None of the corpus suites had a case for any of them.

**Fixtures written inside a test class were converted into broken code (VX032).** marshmallow
writes 16 of them across 9 classes. A velox test class is pure namespacing, so a factory in one
has to be lifted to the module level — `convert/lift.py` now does that, under a name carrying the
class's (`TestLoadOnly.schema` becomes `load_only_schema`), written above the class because a
`Depends()` default is evaluated while the class body runs. The audit reported four of the
sixteen, and only because they happened to override a conftest fixture; the other twelve were
invisible to it, and `convert` emitted signatures velox could not construct.

**A class fixture reading `self` (VX033).** Four of the sixteen read a schema class written in
the class body. Reading it through the class survives the lift and is rewritten that way;
anything else `self` could mean does not, and is now refused with its own row rather than
converted into a `NameError`.

**Relative imports (VX034).** velox imports test modules under synthetic `velox_tests.*` names
with no package behind them, so `from .foo_serializer import ...` cannot resolve — a fact
`layout.dotted` already relied on when writing its own imports, while nothing rewrote the ones the
suite already had. Now a mechanical rule.

**`ignore` could not express a path (velox).** `norecursedirs = ... tests/mypy_test_cases`
converted to `ignore = [..., "tests/mypy_test_cases"]`, which velox matched against bare directory
names only and therefore ignored, collecting two files that are mypy fixtures rather than tests.
`_collection/discovery.py` now matches a `/`-bearing entry the way pytest's `norecursedirs` does.

**Everything a class node broke in the existing machinery.** Seven more defects surfaced once
`classes_showcase` existed and the review went looking, and every one of them is the same
mistake: code that had only ever seen a *directory* as an override node, asking a question about
a file where it should have asked about a node. A test method in a class that overrides was wired
to the original rather than to the copy; a copy in a two-link chain was wired to the definition it
was written against rather than to the copy below it; a copy landed under the class whose methods
read it; the audit's qualname sites stopped matching tables keyed by bare `def` name. The lesson
is narrow and worth keeping: `under(node, consumer)` is only as good as the consumer handed to it.

## The five that still fail

All five are `tests/test_registry.py`, and all five are the same thing: marshmallow's class
registry keys on `cls.__module__`, which under velox is `velox_tests.tests.test_registry` rather
than `tests.test_registry`. The path-derived module name is deliberate — it is what deletes
pytest's `ImportPathMismatchError` — so this is a divergence to name rather than a bug to fix. It
is now `VX035`, an undetectable row the audit reports as a blind spot: no scan can see that a
library three call frames away is reading the test module's name.

## What this says about the tool

The audit's headline number was right about the suite and wrong about the conversion. Every one of
the four constructs was one the matrix had no row for at all, so each read as "converts
untouched" — the failure mode is silence, not a bad classification. The corpus suites were written
alongside the machinery and so only covered what the machinery already knew about; a suite written
by someone else is what finds the gap. That argues for the ladder in the plan being run against
more than one real suite before Phase 4 closes, and it is the reason `classes_showcase` exists.
