# pytest → velox migration: the problem

Internal working document. This states the problem the migration codegen has to solve and the
constraints it operates under; it deliberately proposes no architecture, no tool structure, and no
implementation. It is written against velox as it exists in this tree, plus the near-term entries in
[ROADMAP.md](../ROADMAP.md), and against how pytest suites are actually written in the wild.

---

## 1. What migration has to achieve

velox has no runtime pytest compatibility layer and will not grow one: no `conftest.py`, no
name-based fixture lookup, no hook protocol, no `pytest` import shim. That is what keeps the runner
small and the hot path free of a node tree and hook dispatch. The consequence is
that the *entire* compatibility budget is spent once, in a source-to-source rewrite.

- **Input**: a pytest suite that currently collects and passes, plus its pytest configuration and
  its installed plugins.
- **Output**: a velox suite where every dependency is an imported object named in a `Depends()`
  default; a git diff a human can review; and a report naming everything that was not mechanical.
- **The bar**: fixture wiring needs zero hand edits. Everything else may need review, provided the
  tool says so precisely, *before* the user runs anything.
- **Ranking of failure modes**: silently changing a test's meaning is far worse than refusing to
  convert it. A file the tool declines to touch costs an afternoon. A test that passes for a new
  reason costs trust in the runner, and that is the thing being sold.

This is really **two migrations**, and conflating them is the main way the project fails:

1. **Wiring translation.** Mechanical, statically decidable given pytest's own resolution answers,
   produces a very large diff, and is finished when the suite collects under velox.
2. **Concurrency adaptation.** A pytest suite is written under assumptions velox deletes: one test
   at a time, in file order, with process-global state (env, `sys.modules`, monkeypatched
   attributes, warning filters, the CWD) effectively private to whichever test is running. velox
   runs every test as a concurrent task on one loop. Almost nothing here is decidable from source.

The user-facing ladder that follows from this — migrate, run at `--concurrency 1` (should be
green), then raise concurrency and triage — is not documentation garnish. It is a requirement on the
tool: the output at step 2 must be *exactly* as serial-correct as the pytest original, so that
anything breaking at step 3 is attributable to concurrency and nothing else.

## 2. The target shape

The invariants any rewrite has to land inside, all of them load-bearing in the current tree:

- **Fixtures are objects, imported by name.** `@velox.fixture()` returns a `Fixture`; `Depends()`
  takes that object and raises `TypeError` on anything else. There is no name lookup and no
  fallback, so the import statement *is* the wiring.
- **Injection is read from `__code__`/`__defaults__`,** on the function underneath any decorators
  wrapping the test. Not `inspect.signature`, not annotations. Two hard consequences: `Depends()`
  must sit in default position (a stray one inside `Annotated[...]` is rejected outright), and a
  decorator that fills parameters itself claims the leading ones — `@mock.patch(...)` passes its
  mocks first, positionally, so an injected parameter declared ahead of them is a collection error.
  Rewrites that add decorators must respect this.
- **Scopes are `call`/`function`/`module`/`session`**, and a fixture may depend only on
  equal-or-wider scopes — checked statically at collection, so a bad nesting is a collection error
  rather than a runtime surprise. `module` scope keys on the test's module path; teardown is by
  refcount, not by position in a serial run.
- **Marks are decorators recording a frozen record on the function**: `skip`, `skipif`, `xfail`,
  `parametrize`, `tag`, `timeout`, `solo`, `isolated`. Scalar marks (`skip`, `xfail`, `timeout`)
  raise if applied twice to the same function.
- **Test ids are `relative/path.py::qualname[case-id]`**, rootdir-relative.
- **Both `async def` and plain `def` tests are collected.** A sync test runs on a
  context-propagating executor thread, so a blocking call inside one holds only its own concurrency
  slot rather than the whole loop. Sync tests do *not* have to become async to migrate.
- **`class Test*` is pure namespacing**: its `test_*` methods collect as
  `path.py::TestGroup::test_name`, each called on an instance built for that one test. Nothing in
  velox provides `setup_method`/`teardown_method` or `unittest.TestCase`, and a class carrying
  either is a collection error naming what to move into a fixture.
- **Configuration is `[tool.velox]` in `pyproject.toml`** with six recognized keys — `testpaths`,
  `concurrency`, `timeout`, `test_file_patterns`, `ignore`, `env` — and unknown keys are a hard
  error, not a warning.

## 3. The mapping surface

Everything the codegen can target, grouped by what it can rely on.

**Available today, 1:1 or nearly.** `@pytest.fixture` (`scope`, `name`, yield-teardown, sync and
async, sync- and async-generator bodies) → `@velox.fixture()`; `parametrize` → `@velox.parametrize`
(with `ids`); `@pytest.fixture(params=...)` → `@velox.fixture(params=...)`, `request.param` → the
fixture body's own `param` argument; `skip`/`skipif`/`xfail` marks; custom marks →
`@velox.tag(...)`, selectable with `-m`; `tmp_path`/`tmp_path_factory`; `capsys` → `velox.capture`;
`caplog` → `velox.log_records`; `pytest.raises` (including `match=` semantics, `re.search`, same
escaping gotcha); `pytest.approx` for scalars; `@pytest.mark.asyncio`/`anyio` marks and
`event_loop` fixtures → deleted; `pytest-timeout` → `@velox.timeout(...)`;
`@pytest.fixture(autouse=True)` and `@pytest.mark.usefixtures(...)` → `velox.use(...)` on the
package or module the fixture covered (see §4.3); `@mock.patch`-decorated tests, left as they are
and scheduled solo, with their `Depends()` defaults injected around the mock arguments; `class
Test*` grouping; and the CI invocation surface — `path.py::test_name` ids, `-k`, `-x`/`--maxfail`,
`-v`/`-q`, `--serial`, `--collect-only`, `--durations`.

**On the roadmap, and worth designing against rather than around.** JUnit XML and `--report-json`
(CI consumers depend on these).

**Under review, so plan for its absence.** Lazy or optional dependencies, and overriding one
fixture for a subtree of tests without hand-duplicating its downstream chain, are held pending a
decision on how much implicit specialization velox wants at all
([ROADMAP.md](../ROADMAP.md)). The codegen has to produce a working suite without them, which makes
the specialized chain of §4.2 its baseline output rather than a fallback.

**Never.** `conftest.py`, name-based lookup, `request`, hooks (`pytest_configure`,
`pytest_collection_modifyitems`, `pytest_addoption`, …), plugin entry points, `monkeypatch`,
`pytest.warns`/`recwarn`, imperative `pytest.skip()`/`fail()`/`xfail()`/`importorskip()`, fd-level
capture (`capfd` — velox replaces `sys.stdout`/`sys.stderr`, so a C extension or subprocess writing
to fd 1 is not captured), `pytest.approx` over sequences/dicts/numpy arrays, `unittest.TestCase`,
doctests.

## 4. The five structurally hard problems

Everything in §5 is a lookup table. These five are the actual engineering.

### 4.1 Deciding what a parameter name refers to

This is the core of the tool. A pytest test's parameter list is a set of names resolved at run time
against the union of: fixtures in the same module, fixtures in every `conftest.py` from the test's
directory up to rootdir (nearest wins), fixtures contributed by installed plugins, and pytest's own
builtins. The tool must reproduce that answer for every parameter of every test *and* every fixture,
because getting it wrong produces code that imports the wrong object and still runs.

Constraints worth stating up front:

- The resolution is genuinely dynamic in the general case — conditional fixture definitions,
  `pytest_plugins` declarations, fixtures defined in installed packages, `pytest_generate_tests`.
- Reproducing conftest scoping by static analysis is reimplementing the exact machinery velox
  exists to delete. Whatever the design, the question "does the tool require a working pytest
  collection of the target suite?" is the first fork in the road, and a suite that only collects
  inside a container is a real, common constraint.
- Whatever answers it, the answer must be *per test*, not per name: the same parameter name can
  resolve to different fixtures in two directories, which is §4.2.

### 4.2 Graph specialization: overrides and indirect parametrization

velox's dependency edges are hard-wired at import time: `Depends(session)` names one object,
forever. pytest's edges are resolved per test, late. Two common patterns exploit the difference,
and both collapse into the same problem — **one pytest fixture corresponds to N velox fixture
objects, and every fixture transitively downstream of it must be duplicated too**.

1. **Conftest override / shadowing.** `tests/conftest.py` defines `settings`; `tests/integration/
   conftest.py` redefines `settings`; every fixture that depends on `settings` — `engine`,
   `session`, `client`, …, none of which were redefined — silently picks up the override for tests
   under `integration/`. In velox, redefining the leaf changes nothing for its dependents. Correct
   translation requires generating a specialized *chain* per override scope, and the diff has to
   stay comprehensible while doing it.
2. **`indirect=True` parametrization.** `@pytest.mark.parametrize("backend", [...], indirect=True)`
   chooses a fixture's case per test rather than per fixture — the same multiplication as
   `@pytest.fixture(params=...)`, which maps directly onto `@velox.fixture(params=...)` (§3), but
   selected at the call site instead of the fixture's own declaration, so it still needs a
   generated fixture object per value used.

Case 1 is the problem most likely to be underestimated. Unlike a plain `@pytest.fixture(params=...)`
call, which has a direct target, a conftest override has none: it decides whether migrating a
medium-sized suite produces a 400-line diff or a 40,000-line one.

### 4.3 `autouse`

An `autouse` fixture applies to every test in its visibility scope with nothing written at the call
site. `velox.use(...)` is the counterpart: a declaration on the container rather than on each test,
with the fixture imported and named, so the translation is one line per affected module instead of
one `Depends()` parameter per test. Two of the three things that made this hard fall out of that.
There is no parameter name to invent, because a declared fixture binds to nothing. And ordering
holds: pytest orders autouse fixtures before others at the same scope, and `velox.use` puts its
fixtures earliest in the resolution plan, so they construct first and tear down last.

Visibility lines up too. `autouse` in a `conftest.py` covers a whole directory tree, and a
declaration on that directory's `__init__.py` covers the same tree, so a conftest-level autouse is
one line wherever the conftest was — with the `__init__.py` created if the directory was not
already a package. An `autouse` fixture defined in a test module, and `usefixtures` marks, stay
module-level declarations.

The remaining decision is placement: the same fixture object has to be importable from the
container that declares it, which is the same question §4.5 asks about where translated conftest
fixtures live.

### 4.4 Eliminating `request`

`request` is pytest's escape hatch and has no velox counterpart. Each use is a different problem:

- `request.param` → the fixture body's own `param` argument (see §3), unless the fixture is
  parametrized `indirect=True` from the test rather than declared with `params=` itself (§4.2).
- `request.getfixturevalue("name")` → an explicit `Depends()` when the name is a literal and
  statically resolvable; unresolvable when it is computed, which happens in exactly the
  fixture-factory code that uses it most.
- `request.addfinalizer(fn)` → the `yield`-fixture form; mechanical, but only when the finalizer is
  registered unconditionally.
- `request.node` (name, own marks, `nodeid`), `request.config` (options, ini values),
  `request.cls`/`request.instance`, `request.fixturenames`. Only a slice of this maps to
  `velox.test_info` (`id`, `tags`, `timeout`, `worker`). `request.config.getoption(...)` reaching a
  custom `pytest_addoption` flag has no path at all, because velox has no plugin CLI surface.

### 4.5 Output layout and the import graph

velox suites import fixtures from ordinary modules, so the tool has to *invent a file layout*. It
is not a formatting choice — it decides diff size, review effort, and whether the result reads like
code someone wrote.

- Preserve the conftest layout (one fixture module per directory that had a `conftest.py`) or
  consolidate? Preserving gives a smaller, more reviewable diff and keeps the override structure of
  §4.2 legible; consolidating gives the idiomatic result.
- Name collisions become real: two `conftest.py` files defining `client` are fine in pytest and
  need distinct importable names in velox.
- Fixture modules import application code; test modules import fixture modules; specialized chains
  (§4.2) import each other. Cycles are reachable, and velox resolves imports rootdir-relative with
  namespace-package lookup, no `__init__.py` required — but relative imports between test modules
  do not work, because modules are imported under synthetic `velox_tests.*` names.
- `conftest.py` files also hold non-fixture content — hooks, module-level constants, helper
  functions, `pytest_plugins`. The tool has to split them, and hooks have nowhere to go.

## 5. Mechanical mappings and their traps

The ~80% that is a lookup table — with the traps that make a naive identifier rewrite wrong.

| pytest | velox | Trap |
|---|---|---|
| `@pytest.mark.parametrize(...)` | `@velox.parametrize(...)` | `pytest.param(v, marks=..., id=...)` has no velox spelling: per-case marks do not exist. A per-case `xfail` must become a separate test or a body-level branch. Generated ids differ (velox renders `bool`/`str`/`int`/`None`/enum, else `argname{index}`; floats, bytes, tuples diverge). |
| `@pytest.mark.skipif(cond, reason=)` | `@velox.skipif(cond, reason=)` | velox takes a `bool` or a zero-arg callable; pytest also accepts a *string expression*. Conditions that read module state at import time change meaning if wrapped in a callable (velox evaluates callables at run time, not collection). |
| `@pytest.mark.xfail(...)` | `@velox.xfail(reason, strict=, raises=)` | pytest's `condition` first argument and `run=False` have no equivalent; `run=False` must become a skip. `xfail_strict` from ini has to be materialized per mark. |
| `@pytest.mark.usefixtures("a","b")` | `velox.use(a, b)` | Applies to the whole module, so a mark on one test in a file of many needs the others checked, or the fixture moved to its own module. |
| `pytest.raises(E, match=)` | `velox.raises(E, match=)` | velox refuses to catch `asyncio.CancelledError` (it is the timeout mechanism). The legacy call form `pytest.raises(E, fn, *args)` has no equivalent. |
| `pytest.approx(x)` | `velox.approx(x)` | **Scalars only.** Sequence/dict/numpy comparisons have no equivalent and must be flagged, not rewritten. |
| `capsys.readouterr()` | `velox.capture` | **Semantics differ.** `readouterr()` returns a snapshot *and clears the buffer*; `Capture.out`/`.err` are live, cumulative, never cleared. Any test calling `readouterr()` twice changes meaning under a rename. `capfd`/binary variants have no equivalent at all. |
| `caplog` | `velox.log_records` | `caplog.set_level(...)` is a statement with automatic restore at test end; `LogRecords.set_level(...)` returns a context manager and does nothing until entered — so the rewrite has to restructure the test body. `caplog.records`/`.messages` map cleanly; `.text`, `.record_tuples`, `.clear()`, and the handler-level filtering surface do not. Logger levels are process-global, so this is also a §6 hazard. |
| `tmp_path`, `tmp_path_factory` | same names | `tmpdir`/`tmpdir_factory` are `py.path.local` — a different API (`.join`, `.strpath`, `/`-overloading), so those are a body rewrite, not a rename. |
| `pytest.skip()`, `pytest.fail()`, `pytest.xfail()` inside a body | — | **The most dangerous rename in the whole project.** `velox.skip("reason")` is a decorator factory: as a bare statement it evaluates, returns a function, and does nothing. A blind `pytest.` → `velox.` rewrite converts a conditional skip into a test that silently runs. Must be flagged, never rewritten. |
| `pytest.importorskip("mod")` | — | No equivalent. Closest faithful form is a module-level import guard plus `@velox.skipif`. |
| `pytest.warns`, `recwarn`, `pytest.deprecated_call` | — | No equivalent, and `warnings` filters are process-global — see §6. |
| `monkeypatch` | — | See §6; every use is a semantic decision. |
| `unittest.mock.patch` | left as-is | Keeps working. The decorator form is found at collection and scheduled solo; the context-manager form is invisible until it runs, so velox refuses it at install time and the test fails until it carries `@velox.solo` — the codegen should add that mark at every such site. |

## 6. Hazards the tool must detect and report, never silently paper over

These are behavior changes that migration cannot fix. Each needs to appear in the report with a
count and file/line list, because each is a reason a migrated suite goes red at
`--concurrency 16` after being green at `--concurrency 1`.

- **Test-order dependence.** Tests that pass only because something earlier in the file ran first.
  Undetectable statically in general; detectable empirically (run the pytest suite shuffled).
- **Shared mutable state** — module globals, class attributes, singleton caches, module-level
  registries, `functools.lru_cache` on application code, ORM identity maps.
- **Process-global mutation under concurrency.** The whole `monkeypatch` surface (`setenv`,
  `setattr`, `setitem`, `chdir`, `syspath_prepend`), `mock.patch` in context-manager form,
  `os.environ` writes, `warnings.simplefilter`, `logging` level changes, `sys.modules` surgery,
  `locale`/`decimal` context, freezegun-style global clock patching. In a serial runner all of these
  are private to the running test; under velox they are visible to every test in flight. The velox
  answers are `@velox.solo` (correct, costs wall clock), `@velox.isolated` (a subprocess, correct
  for `chdir`/signals/C-level state), or a DI seam (correct and idiomatic, but a refactor). Choosing
  between them per site is a judgement call the report should surface with enough context that a
  human makes it quickly — including *how much of the suite* ends up serialized, since that number
  is what makes adoption rational or not.
- **Per-test warning filters** — `@pytest.mark.filterwarnings` and `filterwarnings` in ini. The
  warnings module is process-global; per-test filters cannot be honored while tests overlap.
- **Blocking synchronous calls** in test or fixture bodies (sync DB drivers, `requests`,
  `time.sleep`, blocking file/network I/O). One blocking call in an `async def` freezes the whole
  loop and destroys the value proposition. Sync `def` tests are safer — they run on an executor thread — which makes
  "should this sync test become async?" a real question with a wrong answer in both directions.
- **`asyncio.run` / `loop.run_until_complete` / `asyncio.get_event_loop` inside tests or fixtures.**
  These break on a shared loop. Very common in suites that predate `pytest-asyncio`, and common in
  fixtures even in suites that use it.
- **Fixture teardown timing.** pytest tears down a module-scoped fixture when the last test in that
  module finishes, in serial order; velox tears down when the last holder releases, with tests from
  many modules in flight. Anything asserting on teardown having happened, or relying on a
  session-scoped resource being quiescent, changes.
- **Fixed external resources** — a hardcoded port, a shared temp path, a single database, a shared
  cache — which worked because only one test used them at a time.
- **Randomness and seeding** — `random.seed` in a fixture, `faker`, factory sequences: global
  sequence counters that produced deterministic values in serial order no longer do.

## 7. Plugins

Plugin usage is the most common reason a real suite cannot migrate, so the tool has to answer
"can I even do this?" honestly and early — ideally as a pre-flight check, before any rewriting.

Ranked roughly by how often they appear in the async-Python suites velox targets:
`pytest-asyncio`/`anyio` (deleted outright — the point of the exercise), `pytest-mock` (`mocker`
is `mock.patch` with automatic teardown, so it maps to `mock.patch` plus a §6 flag), `pytest-cov`
(a `coverage.py` question, not a fixture question, and one that needs its own answer for a
concurrent single-process runner), `pytest-xdist` (obsolete post-migration, but its
`-n`/`--dist` flags live in CI configs and `worker_id`-keyed fixtures appear in suites),
`pytest-django`/`pytest-flask`/`pytest-postgresql`/`testcontainers` (fixture-heavy, database-heavy,
mostly translatable through §4 machinery plus `exclusive=`), `freezegun`/`time-machine` (global
clock; solo or DI seam), `hypothesis` (`@given` composes with parametrize *and* rewrites the
signature — see the `__defaults__` constraint in §2), `factory-boy` (§6 sequences),
`pytest-httpx`/`respx`/`vcr` (transport-level patching, usually per-test and safe),
`pytest-randomly` (its presence is evidence the suite is order-independent — useful signal),
`pytest-repeat`,
`pytest-subtests` (no equivalent), `pytest-benchmark` (no equivalent, and meaningless under
concurrency).

A per-plugin recipe is only worth writing where a mapping actually exists; for the rest, the
deliverable is a clear "unsupported, here is the manual path, here are the N tests affected".

## 8. Configuration translation

pytest configuration lives in `pytest.ini`, `pyproject.toml`, `setup.cfg`, or `tox.ini`, and CI
invocations carry more of it on the command line. What maps: `testpaths`; `python_files` →
`test_file_patterns`; `norecursedirs` → `ignore`; `pytest-timeout`'s timeout → `timeout`. What has
no home: `addopts` (each flag needs individual treatment), `markers` registration (velox tags need
no declaration), `asyncio_mode`, `xfail_strict` (must be pushed down into each `@velox.xfail`),
`filterwarnings` (§6), `log_cli`, `console_output_style`, `required_plugins`, `pytest_plugins`,
and every option added by `pytest_addoption` in a conftest. Unknown `[tool.velox]` keys are a hard
error, so the tool cannot leave anything speculative in the generated config.

The environment matters too: `env = {...}` in `[tool.velox]` is suite-wide, which is the natural
target for `pytest-env`-style plugins and for a `monkeypatch.setenv` that every test performed
identically.

## 9. Test-id compatibility

CI configs, flaky-test dashboards, `--last-failed` habits, and selective-test-execution tooling all
key on pytest node ids. velox ids are already close (`path::qualname[case]`), and the differences
are: class-based ids lose their middle segment, and generated parametrize ids diverge for anything
that is not `bool`/`str`/`int`/`None`/enum. Since velox's `@velox.parametrize` accepts an explicit
`ids=` tuple, **emitting pytest's own collected ids verbatim** is available and is probably the
right default — it makes id drift a solved problem rather than a documented caveat.

## 10. What "good" looks like for the tool

Properties to design toward, stated as requirements rather than as a design:

- **Comment- and format-preserving.** The output is code humans read in a diff; a reformatted suite
  is an unreviewable one.
- **Idempotent.** Running twice changes nothing the second time. This is what makes a partial
  migration, or a migration on a branch that keeps moving, survivable.
- **No in-place writes without an explicit flag**, and a dry-run mode that reports by category.
- **Every non-mechanical rewrite carries an in-source marker** naming the reason, greppable.
- **The report is half the deliverable.** Before the diff is even applied, a user should know: how
  many tests are mechanical; how many fixtures move and where; how many patch sites exist and how
  each was classified; how much of the suite will run solo or isolated, as a percentage; which tests
  need review and why; and what is outright unsupported. That number — "1.7% of your suite will run
  serially" — is what makes the adoption decision rational.
- **Partial migration is a first-class outcome.** Refusing 40 tests loudly and converting 3,000 is
  a success; converting 3,040 with 12 of them quietly meaning something new is a failure.

## 11. Open questions for whoever designs this

1. **Does the tool require a working pytest collection of the target suite?** Requiring it turns
   §4.1 from "reimplement conftest scoping" into "read pytest's own answer" — but it is a real
   barrier for suites that only collect inside a container or against live infrastructure.
2. **Preserve the conftest layout, or consolidate into one fixture module?** (§4.5)
3. **How far does §4.2's chain specialization go before it is better to fail loudly?** Is there a
   duplication budget past which the tool should stop and ask for a DI seam instead?
4. **Where do the fixtures a `velox.use(...)` line names live**, given the `__init__.py` that
   replaces a `conftest.py` has to import them? (§4.3, §4.5)
5. **Should the tool ever propose a DI seam** — rewriting a patched module global into an injected
   dependency — or only report the opportunity? The idiomatic result needs it; the reviewable diff
   argues against doing it in the same pass.
6. **Is `--concurrency 1` green a gate the tool itself can enforce**, by running the suite before
   and after and comparing outcomes test-for-test? That would turn "trust the rewrite" into a
   checkable property, at the cost of requiring both runners to work in the user's environment.
7. **How is coverage verified across the migration** — same lines covered before and after — and is
   that a tool feature or a documented recipe?
