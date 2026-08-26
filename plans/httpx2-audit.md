# httpx2, audited

Internal working document: the record of Phase 4 steps 0 and 3 of
[migration-tool-plan.md](migration-tool-plan.md) — the scoping spike that decides whether httpx2 is
viable as the exit suite, and the measured audit that replaces the numbers
[oss-refactors-plan.md](oss-refactors-plan.md) inherited from upstream httpx.

Both were run at once, because the spike's first question — *does the fork still parametrize over
trio* — turned out to be one the audit answered wrongly. Four defects were fixed before its
numbers could be quoted.

## What was run

`pydantic/httpx2` at `main`, cloned fresh. It is a monorepo: two workspace members, `src/httpx2`
and `src/httpcore2`, with one `tests/` tree covering both. Set up as marshmallow was, except that
the suite's own environment pins Python 3.10 and velox needs 3.13:

```bash
git clone --depth 1 https://github.com/pydantic/httpx2.git /tmp/httpx2
cd /tmp/httpx2
uv sync --python 3.13                                   # the suite's own dev group
uv pip install -e ~/velox -e ~/velox/velox-migrate      # velox alongside it
uv run velox-migrate extract
uv run velox-migrate audit
```

`uv sync` at the default Python fails to resolve velox at all; the fork supports 3.10 and up,
velox is 3.13 and up, so the extraction environment has to be pinned. That is worth remembering as
a general obstacle: the dump is ground truth *for the environment it ran in*, and for a suite whose
floor is below velox's, the only environment both runners share is the suite's ceiling.

1991 tests collected, against the 539 the selection document extrapolated from upstream httpx. The
baseline is green with the two benchmark modules deselected: **1973 passed, 1 skipped, 17
deselected in 27.3s**, no network-marked test skipped and no plugin missing. That is the number
`verify --record` will take, and the wall clock the converted suite has to beat.

## The verdict

| Outcome | Tests | Share |
|---|---:|---:|
| Collected | 1991 | |
| Converts untouched | 116 | 5.8% |
| Converts with a review marker | 2 | |
| Carries a concurrency hazard | 1531 | |
| Blocked | 342 | 17.2% |
| **Runs serially** | **1753** | **88.0%** |

- 55 test files, 595 of them `async def`; 14 fixtures in the suite, 2 conftest directories
- **0 override chains** — the Phase 3 specialization machinery never fires here either
- Installed plugins: anyio, flaky, pytest-codspeed, pytest-httpbin, pytest-trio

| Code | Disposition | Sites | Tests | What |
|---|---|---:|---:|---|
| VX324 | unsupported | 23 | 294 | `anyio_backend` parametrized over trio |
| VX030 | unsupported | 4 | 29 | `benchmark`/`codspeed_benchmark` (pytest-codspeed), `httpbin`/`httpbin_secure` |
| VX216 | unsupported | 6 | 12 | `pytest.warns`, `deprecated_call` |
| VX108 | unsupported | 6 | 9 | `@pytest.mark.filterwarnings` |
| VX112 | unsupported | 2 | 7 | `@pytest.mark.trio` |
| VX307 | unsupported | 1 | — | `filterwarnings = ["error"]` in `pyproject.toml` |
| VX402 | hazard | 5 | 1739 | `os.environ` writes, autouse in the root conftest |
| VX413 | hazard | 89 | 133 | a fixed host and port |
| VX401 | hazard | 4 | 37 | monkeypatch |
| VX405 | hazard | 6 | 13 | `sys.modules` surgery |
| VX205 | marker | 4 | 5 | `caplog.set_level` |
| VX218 | mechanical | 4 | 5 | `mock.patch` as a context manager |
| VX008 | mechanical | 2 | 1739 | autouse fixtures |

## The two questions the spike existed to ask

**Does the fork still parametrize over trio?** Yes, and far more than upstream's numbers implied.
The suite carries no `pytest.mark.parametrize` over backends: anyio's own `anyio_backend` fixture
is parametrized `("asyncio", "trio")` whenever trio is installed, and it hangs a
`usefixtures("anyio_backend")` mark on every test it marks. **294 of the 1991 collected cases are
the trio half of that matrix** — every `[trio]` id in the suite is anyio's doing, not the suite's.
On top of that, 7 tests carry `@pytest.mark.trio` and go through pytest-trio directly.

301 cases, 15% of the suite, run on a loop velox does not have.

**Has the fork changed the conftest/fixture graph?** Not in a way that matters. Two conftests
(`tests/httpx2/`, `tests/httpx2/websockets/`), 14 fixtures, no overrides, one `request.param` read
that converts. The wiring is simpler than flask's. What the fork *did* change is scale: `httpcore2`
is vendored into the same tree, its `_sync` half generated from its `_async` half, so every
connection-pool test exists four times over — sync, async, and each of those on both backends.

## What the spike found in the tool

The first audit run reported 233 findings over 19 constructs. 161 over 17 survived a look at what
the sites actually were. Four defects, all of them the same shape — a signal read from a name
without asking who wrote it:

**`request` is a domain object in an HTTP suite (VX015 ×64, VX017 ×10).** The source scan treated
any function with a parameter named `request` as a fixture, so httpx2's mock applications
(`def __call__(self, request: httpx2.Request)`) and its `Auth.auth_flow` implementations read as
fixtures holding pytest's request past setup — 245 and 113 tests between them, none of them true.
pytest fills a function's parameters in only for a fixture factory or a test, so `_takes` now
resolves the innermost `def` that declares the name and asks whether pytest is what calls it. The
suite's one genuine use, `request.param` in a params fixture, is a row that converts and was never
reported.

**anyio's `usefixtures` marks read as the suite's (VX009 ×10, VX010 ×13).** 883 tests were reported
as needing a `velox.use(...)` declaration placed for them. httpx2 does not write the word
`usefixtures` anywhere: every one of those marks is anyio's wiring for a fixture whose whole job
velox does itself. `marks` now skips a `usefixtures` naming a fixture some plugin provides and
migration deletes.

**pytest-trio's mark was invisible (VX323).** The plugin row said "No test requests its fixtures or
carries its marks" while 7 tests carried `@pytest.mark.trio`. `trio` was in `KNOWN_MARKS` — the
marks pytest or the runner answers — beside `asyncio` and `anyio`. It does not belong there:
velox answers those two by running the test, and answers `trio` by running it on the wrong loop.
Moved to `PLUGIN_MARKS`, which classifies it `VX112` per test and refuses it in `convert`.

**The trio half of the case list was reported by nothing at all (new: VX324).** anyio is a
`VX320` plugin — "its whole job is done by the runner" — which is true of running a test and false
of parametrizing one onto a backend. Every `[trio]` case counted as converting untouched. There is
now a row for a case a backend fixture puts on a loop velox does not run, reported per module,
with the 294 cases attributed.

Each was invisible to the corpus, which has no async plugin in it at all; `tests/test_audit_async.py`
grafts anyio's wiring onto a corpus dump rather than adding a suite, since what needed pinning is
what the plugin does to a dump.

**Not fixed: VX413 over-reports on a URL-heavy suite.** 89 of its sites are strings like
`"http://localhost:8080/"` handed to a *mock* network backend — no port is ever bound. The row's
heuristic is a host-and-port literal, and narrowing it to calls that bind would miss the ones that
matter. `reach.py` already states the bias — "over-reporting a hazard costs a reader an
inspection, under-reporting costs them a red suite" — so this stays as it is, and a reader of an
HTTP suite's audit should expect the row to be noise.

## Is httpx2 viable as the exit suite?

Yes, with one prefactor named up front, and it is a better exit suite than the numbers suggest.

The 88% serial estimate is *one autouse fixture*: `clean_environ` in `tests/httpx2/conftest.py`
snapshots and rewrites `os.environ` for 1739 of 1991 tests. That is flask's arc exactly — naive
conversion serializes the suite, and unwinding one fixture is what recovers concurrency — which is
the arc Phase 4 exists to document, now on a suite four times flask's size.

The trio half is the real cost, and it is prefactorable in pytest terms: a suite-level
`anyio_backend` fixture returning `"asyncio"` pins the matrix, keeps the suite green under pytest,
and drops the ids' `[asyncio]`/`[trio]` suffixes on both sides of `verify` at once. It is a
one-fixture change rather than a codemod, so **step 4 may still not need `prefactor/` to exist** —
the same conclusion the plan reached from upstream's caplog numbers, by a different route.

What stays out of the migration either way: 34 pytest-codspeed benchmark tests, 6 pytest-httpbin
tests, 7 `@pytest.mark.trio` tests, 12 `pytest.warns` tests, 9 `filterwarnings` tests. Around 68
cases, 4% of what is left after the trio half — small enough to name in the write-up.

The suite-wide `filterwarnings = ["error"]` (VX307) is the one to watch. It has no velox spelling,
so a warning httpx2 currently treats as a failure becomes a warning again after conversion, and a
test that passes for that reason will pass under velox for a different one. Nothing in `verify`
catches that; it belongs in the write-up beside the blind spots.
