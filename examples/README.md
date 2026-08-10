# velox examples

Worked examples of the consumer-facing syntax specified in [`spec/01-public-api.md`](../spec/01-public-api.md),
dogfooded against the actual M1 runner — `uv run velox` passes, green, in each subfolder today
(docs/M1-PLAN.md tracks this). They were originally written ahead of the runner, as syntax to read
and argue with before the API was hard to change; that's still true of a few specific features each
one leans on that M1 hasn't built yet (`@velox.parametrize` expansion, class grouping, `@velox.xfail`
execution, `exclusive=`/`@velox.solo` enforcement, the loop-starvation watchdog, most of the CLI
beyond a bare path and `--concurrency`/`--timeout`/`-s`/`--assert`/`--basetemp`) — each example's own
README has a "Known gaps" section naming exactly which, and routes around them so the suite still
runs correctly and safely in the meantime, rather than showing code that would collection-error,
silently under-test, or race.

Each subfolder is self-contained: application mockup, test suite, config, and the commands you would
run against it.

---

## The three use cases

### [01 — FastAPI + async SQLAlchemy CRUD](01-fastapi-crud/)

**The reference stack, and the case velox exists for.** A FastAPI app over async SQLAlchemy, tested
through `httpx.AsyncClient` against a real database.

Shows: session-scoped `engine`, the transaction-rollback `session` fixture, one shared `app`
instance with `dependency_overrides`/`state` layered per test via `velox.fastapi.client` (not a
per-test app rebuild — that's the point), a sibling fixture to swap one node of the graph for one
test (`Fixture.with_()` would do this automatically, but it is roadmap — spec/01 §10),
`velox.raises`, and one `exclusive=` external sandbox (declared; not yet scheduler-enforced — see
the example's own "Known gaps"). `@velox.parametrize` and class grouping are used in the spec
sense but not yet expanded/collected by the runner, so the suite spells both out by hand instead.

The point: **one engine, N concurrent tests.** Under `pytest-xdist` this suite is four processes,
four engines, four connection pools, and four copies of every import. Here it is one process, one
engine, and the tests are `asyncio` tasks.

Needs `requirements.txt` (fastapi, sqlalchemy, aiosqlite, httpx).

### [02 — Pure-async library, and the mocking ladder](02-async-library/)

**A library with no I/O dependencies**, tested with no dependencies either — stdlib only.
A webhook delivery client with retry, jitter, and a TTL cache.

Shows: the three tiers of [`spec/08`](../spec/08-patching-and-isolation.md), designed to sit side
by side in one suite —

- **tier (a)** dependency injection, fully concurrent, and what you get when a seam already exists
  (the cache's clock is a constructor argument, so no patching is needed at all) — this tier is
  real today, and it's most of what runs live in this suite;
- **tier (b)** stock `unittest.mock`, which the plan is for velox to detect and schedule **solo**,
  reporting the cost in the run summary — not built yet (the detection, the scheduling, and the
  summary line are all roadmap), so this suite's tier (b) tests are marked `skip` rather than run
  unguarded against each other's shared patch targets;
- **tier (d)** `@velox.isolated`, a subprocess escape hatch for state with no per-task view at all
  (`chdir`) — also roadmap, also skipped rather than run for real against the one shared process.

Also: `velox.log_records`, `velox.raises`, `velox.approx`, `@velox.timeout`, `velox.tmp_path`, and a
sync test running on the executor — all real today.

The point: **most mocking should not be mocking.** Tier (a) is the concurrency-safe majority; tiers
(b)/(d) are what's left over, and the example's own README explains exactly what running them live
would take that doesn't exist yet.

Standalone — no `requirements.txt`.

### [03 — Shared resources, exclusion, and the safety net](03-shared-resources/)

**What concurrency actually breaks, and the primitives for it.** A ledger over stdlib `sqlite3`,
plus a webhook receiver that binds a fixed port.

Shows the *design* of `exclusive=True` and `exclusive="token"`, a test whose footprint would be
*two* tokens admitted atomically (see [`spec/06 §2`](../spec/06-scheduling-and-determinism.md)),
`@velox.solo` for a process-global mutation, and the loop-starvation watchdog that would catch a
blocking `sqlite3` call never wrapped in `asyncio.to_thread`. None of the scheduling behind any of
that is built yet — `_run.py`'s own module docstring lists exclusive-resource admission and the
solo tier as still deferred, and there is no watchdog at all — so this example runs everything
that's actually safe without it (data-isolated tests, and resources that don't really collide even
though they carry a token) and marks `skip` on the two spots where the underlying conflict is real
(a fixed TCP port, a shared feature flag) rather than let them race. The blocking-call test still
runs, and still stalls the loop for real; nothing reports it yet. See the example's own README.

The point: **resource conflicts are declared on the resource, not worked around in the scheduler.**
That's still true of the design; "the scheduler" for most of it doesn't exist yet.

Standalone — no `requirements.txt`.

---

## Reading order

If you are reviewing the API, read them in order: 01 establishes the shape, 02 stresses the parts
that are contentious (mocking, solo cost), 03 covers the parts that only exist because velox is
concurrent.

If you are evaluating whether to adopt, read 01 and then the "What this costs you" section of 03.

## Running them

```bash
cd examples/01-fastapi-crud
uv venv && uv pip install -r requirements.txt -e ../..
velox
```

`02-async-library` and `03-shared-resources` are standalone (`uv venv && uv pip install -e ../..`,
no `requirements.txt`). Every example's README lists its own commands — the real, current ones,
not the eventual full CLI — and real output captured from an actual run.

---

## Notes from writing the spec, and from dogfooding it afterwards

Writing these originally, ahead of the runner, surfaced things the spec didn't answer yet — most
now resolved one way or another, noted inline below. Running them against the real M1 runner
afterwards surfaced a second round, tracked per-example in each README's "Known gaps" section and
centrally in [`docs/M1-PLAN.md`](../docs/M1-PLAN.md) rather than repeated here.

1. **Resolved.** *How does `tests/test_users.py` import `tests/fixtures.py`?* Answered by the
   rootdir import convention: `cli.main` prepends the rootdir to `sys.path[0]` exactly once at
   startup, so a plain absolute import (`from tests.fixtures import api_client`) resolves via
   ordinary PEP 420 namespace-package lookup — no `__init__.py`, no per-conftest-directory games.
   Written down in spec/00 §11, spec/02 §5, spec/03 §3; shipped (`docs/M1-PLAN.md`).
2. **`velox.tmp_path_factory` has no named type.** Used here as `velox.TmpPathFactory` with a
   `.mktemp(name)` method, matching pytest.
3. **Resolved, differently than first proposed.** *`spec/01 §3`'s original `api_client` example
   mutated a module-level `app` directly* — the exact footgun [`spec/08 §3`](../spec/08-patching-and-isolation.md)
   warns about, since `dependency_overrides` is per-app-instance state. The fix that shipped isn't
   an app factory (this note's original guess): it's `velox.fastapi.client`, a `ContextVar`-layered
   proxy over `app.dependency_overrides`/`app.state` installed once per app object, so the app
   stays exactly the module-level singleton production code already has and concurrent tests each
   read/write their own layer through it. spec/01 §3 shows the shipped shape now.
4. **`Fixture.with_()` written inline inside `Depends(...)` trips ruff's B008**, and unlike
   `Depends` itself there is no qualified name to add to `extend-immutable-calls` — it is a method
   on an arbitrary object. Binding derived fixtures to module-level names
   (`flaky_relay = relay.with_(transport=flaky_transport)`) dodged the lint but not the deeper
   problem underneath it: this finding is one of the two reasons `Fixture.with_()` ended up
   deferred to roadmap rather than shipped ([`spec/01 §10`](../spec/01-public-api.md)), the other
   being a caching-identity bug at module/session scope. The examples now write the sibling
   fixture directly — `flaky_relay` is its own `@velox.fixture()` in `tests/fixtures.py` — which
   never trips B008 in the first place, since a function definition is not a call expression in
   an argument default.

   Every example's `pyproject.toml` also carries
   `extend-immutable-calls = ["velox.Depends"]`; without it B008 fires on every test in the suite.
   That line belongs in the getting-started docs.
