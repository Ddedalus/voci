# pytest → voci migration: findings

Internal working document, and the evidence half of
[migration-tool-plan.md](migration-tool-plan.md): the corpus that was picked to prove the tool
against, what each run of it measured, and what those runs said about the tool itself. The plan
holds work; this holds what is known. Nothing here is a task.

## What the two real runs concluded

Two suites have been through the tool end to end or most of the way, and between them they name
the two things standing between "the pipeline runs" and "a suite is migrated".

**The audit's failure mode is silence, not misclassification.** marshmallow's audit came back
clean and the conversion emitted code voci could not construct. Every one of the four defects was
a construct the support matrix had no row for at all, so each read as *converts untouched* — a
number in the headline that meant "nothing recognised it" rather than "nothing wrong with it". The
corpus suites were written alongside the machinery and cover what the machinery already knew about;
a suite written by someone else is what finds the gap. Any suite the matrix was not written against
gets an audit whose clean count is an upper bound of unknown tightness.

**Mechanical success does not deliver concurrency.** httpx2 converts to a suite that is 88% serial,
and the whole 88% is one autouse fixture writing `os.environ`. flask (77 monkeypatch sites behind
one autouse fixture) and rich (one autouse `reset_color_envvars`) are the same shape. A conversion
that is byte-correct still hands back a suite that runs at pytest's speed until that one fixture is
unwound, which is a rewrite the tool does not perform and, at present, does not have a rule for.

The corpus's other blind spot is structural: neither marshmallow nor httpx2 has a single conftest
override, so the Phase 3 specialization machinery — the most novel and least prior-art-backed thing
in the tool — has never fired against a suite it was not written for.

## The corpus

Ten popular OSS projects were scanned across the support matrix's refusal and serialization rows.
Four were viable; the rest are blocked on constructs with no conversion path.

### marshmallow — the clean proof

| Metric | Value |
|---|---|
| Test count | 1188 collected |
| Fixture graph | 19 fixtures / 1 conftest, 0 overrides |
| Plugins | none |
| Estimated refusals | none |
| Monkeypatch uses | 0 |

Picked as the smoke test precisely because it is clean: if marshmallow's audit came back
non-mechanical, the tool had a bug.

### httpx2 — the exit suite

`pydantic/httpx2`, not upstream httpx. Four times flask's size, the same concurrency arc, a real
plugin-wired async story, and no override chains.

| Metric | Value |
|---|---|
| Test count | 1991 collected, 1973 green under pytest in 27.3s |
| Fixture graph | 14 fixtures / 2 conftests, 0 overrides |
| Plugins | anyio, pytest-trio, pytest-httpbin, pytest-codspeed, flaky |
| Blocked | 342 tests (17.2%), 294 of them the trio half of `anyio_backend` |
| Serializing hazards | VC402 ×5 — one autouse `clean_environ` reaches 1739 tests |
| Naive serial share | 88.0% |

### flask — the ladder

| Metric | Value |
|---|---|
| Test count | 386 (31 parametrized, 7 classes) |
| Fixture graph | 17 fixtures / 1 root conftest + 2 example conftests |
| Plugins | coverage only |
| Estimated refusals | VC210 ×19, VC401 ×77, VC405 ×6, VC017 ×1 |
| Monkeypatch uses | 77 |

`_standard_os_environ(monkeypatch)` is autouse in the root conftest, so the whole suite serializes
under naive rules. 19 sites of the legacy `pytest.raises(E, func, ...)` call form are the textbook
prefactor target. `test_apps` and `purge_module` do `sys.modules` surgery and hold `request` in
closures for `addfinalizer`, which are honest refusals. Superseded as the exit suite by httpx2,
which has the same arc at four times the scale; retained as a second real suite.

### rich — the volume check

| Metric | Value |
|---|---|
| Test count | 721 (35 parametrized, 0 classes) |
| Fixture graph | 5 fixtures / 1 conftest |
| Plugins | pytest-cov only |
| Estimated refusals | none found |
| Monkeypatch uses | 19 |

Same 100%-serialize trap as flask from a single autouse `reset_color_envvars(monkeypatch)`, but
trivially unwound. Almost no fixture graph. Its value is scale: before/after concurrency of
100% → ~99% serial with 19 tests to clean up.

### jinja — the middle ground

| Metric | Value |
|---|---|
| Test count | 691 (32 parametrized, 45 classes) |
| Fixture graph | 21 fixtures / 1 conftest |
| Plugins | pytest-timeout, trio |
| Estimated refusals | VC014 ×1 |
| Monkeypatch uses | 6 |

One fixture returns `request.param` over parametrized `_asyncio_run` and `trio.run`, which exercises
the indirect-parametrization path. One `request.addfinalizer` refuses.

### Skipped

| Suite | Blocker |
|---|---|
| starlette | `tmpdir` ×130 (VC208). Needs a `tmpdir→tmp_path` prefactor codemod to exist first. |
| structlog | `pytest.warns` ×56 (VC216, unsupported *and* serializing), time-machine (VC408), `asyncio_mode = auto`, pytest-randomly. |
| attrs | hypothesis (VC030/VC323), `pytest_configure` hook (VC022). |
| click | `capfd` ×40 (VC203, no equivalent), pytest-randomly. |
| uvicorn | pytest-mock (VC219), `pytest.param(marks=)` ×12 (VC102), tests binding real network ports (VC413). |

## marshmallow, migrated

The record of the first end-to-end run against a suite the tool was not built against.
`oss/marshmallow` at `c7b559a`, green under pytest at 1188 tests — not the 652 the selection scan
extrapolated, which counted test *functions* rather than collected cases.

| Runner | Passed | Failed | Wall |
|---|---:|---:|---:|
| pytest | 1188 | 0 | 0.77s |
| `voci --serial` | 1183 | 5 | 2.12s |
| `voci` (11.4x) | 1183 | 5 | 0.69s |

Nothing refused, no `VOCI-TODO` markers written, and concurrency cost nothing: the same five tests
fail serially and in parallel, so none of the 203 tests the audit flagged under VC412 (seeded
randomness) actually depends on running alone.

### Environment obstacles worth remembering

`convert --write` rewrites the tree in place, so the suite has to be copied out of `oss/` first —
those are read-only submodules pinned at a commit. The suite's own `uv run pytest` will not do
either, since that resolves a lockfile that knows nothing about voci; the environment needs
`-e .`, `-e ~/voci` and `-e ~/voci/voci-migrate` together. `convert --write` writes the whole
tree before printing a line of the plan or the diff, so quitting the pager early still leaves the
converted suite on disk.

### What the bug hunt found

Four defects in what the suite itself needed, and seven more behind them once the machinery had a
class-shaped override node to work with. No corpus suite had a case for any of them.

**Fixtures written inside a test class were converted into broken code (VC032).** marshmallow writes
16 of them across 9 classes. A voci test class is pure namespacing, so a factory in one has to be
lifted to module level — `convert/lift.py` does that under a name carrying the class's
(`TestLoadOnly.schema` becomes `load_only_schema`), written above the class because a `Depends()`
default is evaluated while the class body runs. The audit reported four of the sixteen, and only
because they happened to override a conftest fixture; the other twelve were invisible to it.

**A class fixture reading `self` (VC033).** Four of the sixteen read a schema class written in the
class body. Reading it through the class survives the lift; anything else `self` could mean is
refused with its own row rather than converted into a `NameError`.

**Relative imports (VC034).** voci imports test modules under synthetic `voci_tests.*` names with
no package behind them, so `from .foo_serializer import ...` cannot resolve — a fact `layout.dotted`
already relied on when writing its own imports, while nothing rewrote the ones the suite already had.

**`ignore` could not express a path (voci itself).** `norecursedirs = ... tests/mypy_test_cases`
converted to `ignore = [..., "tests/mypy_test_cases"]`, which voci matched against bare directory
names only. `_collection/discovery.py` now matches a `/`-bearing entry the way pytest's
`norecursedirs` does.

**Everything a class node broke in the existing machinery.** Seven more defects surfaced once
`classes_showcase` existed, every one the same mistake: code that had only ever seen a *directory*
as an override node, asking a question about a file where it should have asked about a node. The
lesson is narrow and worth keeping: `under(node, consumer)` is only as good as the consumer handed
to it.

### The five that still fail

All five are `tests/test_registry.py`, and all five are the same thing: marshmallow's class registry
keys on `cls.__module__`, which under voci is `voci_tests.tests.test_registry` rather than
`tests.test_registry`. The path-derived module name is deliberate — it is what deletes pytest's
`ImportPathMismatchError` — so this is a divergence to name rather than a bug to fix. It is `VC035`,
an undetectable row the audit reports as a blind spot: no scan can see that a library three call
frames away is reading the test module's name.

marshmallow was corpus-ified as `classes_showcase` rather than as a checked-in dump — the dump is
2 MB per pytest version against 504 KB for the whole existing corpus, and what marshmallow exercised
is what a showcase suite pins.

## httpx2, audited

The scoping spike and the measured audit, run at once because the spike's first question — *does the
fork still parametrize over trio* — turned out to be one the audit answered wrongly.

`pydantic/httpx2` at `main`, cloned fresh. It is a monorepo: two workspace members, `src/httpx2` and
`src/httpcore2`, with one `tests/` tree covering both. The suite's own environment pins Python 3.10
and voci needs 3.13, so the extraction environment has to be pinned with `uv sync --python 3.13`.
That generalizes: the dump is ground truth *for the environment it ran in*, and for a suite whose
floor is below voci's, the only environment both runners share is the suite's ceiling.

### The verdict

| Outcome | Tests | Share |
|---|---:|---:|
| Collected | 1991 | |
| Converts untouched | 116 | 5.8% |
| Converts with a review marker | 2 | |
| Carries a concurrency hazard | 1531 | |
| Blocked | 342 | 17.2% |
| **Runs serially** | **1753** | **88.0%** |

55 test files, 595 of them `async def`; 14 fixtures, 2 conftest directories, 0 override chains.

| Code | Disposition | Sites | Tests | What |
|---|---|---:|---:|---|
| VC324 | unsupported | 23 | 294 | `anyio_backend` parametrized over trio |
| VC030 | unsupported | 4 | 29 | `benchmark`/`codspeed_benchmark`, `httpbin`/`httpbin_secure` |
| VC216 | unsupported | 6 | 12 | `pytest.warns`, `deprecated_call` |
| VC108 | mechanical | 6 | 9 | `@pytest.mark.filterwarnings` |
| VC112 | unsupported | 2 | 7 | `@pytest.mark.trio` |
| VC307 | mechanical | 1 | — | `filterwarnings = ["error"]` in `pyproject.toml` |
| VC402 | hazard | 5 | 1739 | `os.environ` writes, autouse in the root conftest |
| VC413 | hazard | 89 | 133 | a fixed host and port |
| VC401 | hazard | 4 | 37 | monkeypatch |
| VC405 | hazard | 6 | 13 | `sys.modules` surgery |
| VC205 | marker | 4 | 5 | `caplog.set_level` |
| VC218 | mechanical | 4 | 5 | `mock.patch` as a context manager |
| VC008 | mechanical | 2 | 1739 | autouse fixtures |

These counts predate `@voci.filterwarnings` and `[tool.voci] filterwarnings`, which carry VC108
and VC307 across mechanically. Re-run the audit before converting to get the totals under the
current matrix.

### The two questions the spike existed to ask

**Does the fork still parametrize over trio?** Yes, and far more than upstream's numbers implied.
The suite carries no `pytest.mark.parametrize` over backends: anyio's own `anyio_backend` fixture is
parametrized `("asyncio", "trio")` whenever trio is installed, and it hangs a
`usefixtures("anyio_backend")` mark on every test it marks. 294 of the 1991 collected cases are the
trio half of that matrix — every `[trio]` id in the suite is anyio's doing, not the suite's. On top
of that, 7 tests carry `@pytest.mark.trio` and go through pytest-trio directly. 301 cases, 15% of
the suite, run on a loop voci does not have.

**Has the fork changed the conftest/fixture graph?** Not in a way that matters. Two conftests, 14
fixtures, no overrides, one `request.param` read that converts. The wiring is simpler than flask's.
What the fork *did* change is scale: `httpcore2` is vendored into the same tree, its `_sync` half
generated from its `_async` half, so every connection-pool test exists four times over.

### What the spike found in the tool

The first audit run reported 233 findings over 19 constructs; 161 over 17 survived a look at what
the sites actually were. Four defects, all the same shape — a signal read from a name without asking
who wrote it.

**`request` is a domain object in an HTTP suite (VC015 ×64, VC017 ×10).** The source scan treated
any function with a parameter named `request` as a fixture, so httpx2's mock applications
(`def __call__(self, request: httpx2.Request)`) and its `Auth.auth_flow` implementations read as
fixtures holding pytest's request past setup — 245 and 113 tests between them, none of them true.
`_takes` now resolves the innermost `def` that declares the name and asks whether pytest is what
calls it.

**anyio's `usefixtures` marks read as the suite's (VC009 ×10, VC010 ×13).** 883 tests were reported
as needing a `voci.use(...)` declaration. httpx2 does not write the word `usefixtures` anywhere:
every one of those marks is anyio's wiring for a fixture whose whole job voci does itself. `marks`
now skips a `usefixtures` naming a fixture some plugin provides and migration deletes.

**pytest-trio's mark was invisible (VC323).** `trio` sat in `KNOWN_MARKS` — the marks pytest or the
runner answers — beside `asyncio` and `anyio`. It does not belong there: voci answers those two by
running the test, and answers `trio` by running it on the wrong loop. Moved to `PLUGIN_MARKS`.

**The trio half of the case list was reported by nothing at all (new: VC324).** anyio is a `VC320`
plugin — "its whole job is done by the runner" — which is true of running a test and false of
parametrizing one onto a backend. Every `[trio]` case counted as converting untouched.

Each was invisible to the corpus, which has no async plugin in it at all; `tests/test_audit_async.py`
grafts anyio's wiring onto a corpus dump rather than adding a suite, since what needed pinning is
what the plugin does to a dump.

### Two knowingly unfixed

**VC324 reads callspecs, so a backend chosen in a fixture body is invisible.** A suite whose own
`anyio_backend` returns `"trio"` unparametrized runs entirely on trio with nothing reported — the
value is in a function body, and the source scan has no notion of which fixture decides a loop. The
shape that matters is the parametrized one, which is anyio's default. Worth a row of its own only if
a suite is found that pins the wrong backend deliberately.

**VC413 over-reports on a URL-heavy suite.** 89 of its sites are strings like
`"http://localhost:8080/"` handed to a *mock* network backend — no port is ever bound. Narrowing the
heuristic to calls that bind would miss the ones that matter, and `reach.py` already states the bias:
over-reporting a hazard costs a reader an inspection, under-reporting costs them a red suite. A
reader of an HTTP suite's audit should expect the row to be noise.

### Viability as the exit suite

Yes, with one prefactor named up front. The 88% serial estimate is *one autouse fixture*:
`clean_environ` in `tests/httpx2/conftest.py` snapshots and rewrites `os.environ` for 1739 of 1991
tests. That is flask's arc exactly, on a suite four times the size.

The trio half is the real cost, and it is prefactorable in pytest terms: a suite-level
`anyio_backend` fixture returning `"asyncio"` pins the matrix, keeps the suite green under pytest,
and drops the `[asyncio]`/`[trio]` id suffixes on both sides of `verify` at once. That is a fixture
to write rather than a codemod to run.

What stays out of the migration either way: 34 pytest-codspeed benchmark tests, 6 pytest-httpbin
tests, 7 `@pytest.mark.trio` tests, 12 `pytest.warns` tests — around 59 cases, 3% of what is left
after the trio half.
