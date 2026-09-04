# `_collection/collect.py` — import and collection

See [rationale.md](../rationale.md) for the index.

**Each test file is imported under a synthetic module name.** The name is derived from the path
relative to rootdir, and `sys.path` is never touched. This is what lets two `test_utils.py` files in
different directories coexist in one run — they get different dotted names instead of colliding in
`sys.modules`. The escaping matters for the same reason: `api-v2` and `api_v2` must not collapse to
the same identifier, so an escaped segment carries a short content digest. Simplify that to a plain
`str.replace` and the collisions come back.

**The rewrite hook is consulted by hand during import.** `importlib.util.spec_from_file_location`
never looks at `sys.meta_path`, so building a spec that way silently skips assertion rewriting no
matter how it was installed. `_import_module` therefore asks the installed hook's `find_spec`
directly before falling back. Remove that call and rewriting stops working for every test while the
reported mode still says `rewrite` — no exception, no failing test, just worse assertion messages.

**A class-grouped test gets its receiver built per call, through a hand-written wrapper.** A
`class Test*` is namespacing and nothing else, so the instance a method runs on is constructed for
that one test and discarded — anything shared through `self` would be shared between concurrently
running tests, which is the one thing the grouping must not buy. The wrapper that does this copies
`__name__`, `__qualname__`, `__wrapped__` and the marks by hand rather than using
`functools.wraps`, which copies `__dict__` wholesale: `mock.patch` keeps its `patchings` list
there, and a copy of it on the wrapper is counted a second time by `patching_of`, doubling both
the reported patch targets and the positional arguments they are taken to supply — which then
hides a real missing injection behind a parameter velox believes a mock will fill.

**A test shape that would collect as nothing is a collection error.** A `Test*` class velox can't
construct, a `setup_method` that would never run, a mark on a class, a `test_*` name bound to a
lambda, a test that yields — each of these is silent in the worst way: the suite looks green
because tests are missing from it, or because a test ran without the setup it was written to
expect. Every rule for reporting them is deliberately narrow, because the cost of a false report
is a collection error on working code: a class is reported for being misnamed only when it reads
as a suite (`unittest.TestCase`, or a name ending in `Test`/`Tests`/`TestCase`) *and* no group
inherits it, and a `test_*` name is reported only when it is bound to a function — `test_app =
FastAPI()` and `test_client = Mock()` are callable, ordinary, and nobody's test body.

**A group's shape is read across its whole MRO.** Test methods, `__init__` and lifecycle hooks
are all resolved the way an attribute lookup would resolve them, not off the class body alone.
Reading only `vars(cls)` silently drops every test a shared base contributes — the standard
"one suite, run against three backends" layout — and lets an inherited `setup_method` through the
guard whose entire job is catching setup that will never run.

**`@velox.parametrize` shares one resolution plan across every expanded case.** `plan_for` runs
once per test function, not once per case: a parametrized value is a call kwarg, not a DI graph
node, so building the plan per case would repeat identical work for nothing. `parametrize.
known_params_of` computes what names parametrize supplies before that one `plan_for` call, so
`_check_missing_injections` doesn't mistake a case's own arguments for uninjected fixtures — and
the same pass rejects a name two stacked `@parametrize`s both claim, or one that collides with an
actual `Depends(...)` injection, since either would otherwise fail confusingly later, at call time,
once two sources tried to supply the same keyword.

**A collected record carries its own marks, and every condition on them is already decided.**
`velox.case(value, marks=...)` puts a mark on one case of a `@velox.parametrize`, so the marks
reaching one record of a test function need not be the marks reaching the next. Collection folds
the case's marks into the function's and stores the result on `TestRecord`, which is what the
runner reads — a mark answered by looking at `func` would be answered once for cases that differ.
The same pass decides `@velox.skipif`'s and `@velox.xfail(condition=...)`'s conditions, both of
which may be callables: evaluating a condition is running suite code, and running it inside the
runner would mean a user's expression raising in the middle of concurrent dispatch rather than
becoming this test's collection error alongside every other malformed mark. A `-m` tag expression
therefore waits for the expansion whenever some case carries a tag of its own, since the
function's tags are then not the answer for any of its cases.
