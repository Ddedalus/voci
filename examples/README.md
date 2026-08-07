# velox examples

Status: AI slop.

Worked examples of the consumer-facing syntax specified in [`spec/01-public-api.md`](../spec/01-public-api.md).

> **These do not run yet.** velox is a specification with a walking skeleton behind it; the
> `import velox` in these files resolves to a package that does not implement `fixture`, `Depends`,
> or the marks. The examples exist to make the syntax concrete — to be read, argued with, and
> revised — *before* the API is hard to change. Everything else in each example (the application
> code, the `pyproject.toml`, the commands) is real.

Each subfolder is self-contained: application mockup, test suite, config, and the commands you would
run against it.

---

## The three use cases

### [01 — FastAPI + async SQLAlchemy CRUD](01-fastapi-crud/)

**The reference stack, and the case velox exists for.** A FastAPI app over async SQLAlchemy, tested
through `httpx.AsyncClient` against a real database.

Shows: session-scoped `engine`, the transaction-rollback `session` fixture, a per-test app instance
with `dependency_overrides`, `Fixture.with_()` to swap one node of the graph for one test,
`@velox.parametrize`, class grouping, `velox.raises`, and one `exclusive=` external sandbox.

The point: **one engine, N concurrent tests.** Under `pytest-xdist` this suite is four processes,
four engines, four connection pools, and four copies of every import. Here it is one process, one
engine, and the tests are `asyncio` tasks.

Needs `requirements.txt` (fastapi, sqlalchemy, aiosqlite, httpx).

### [02 — Pure-async library, and the mocking ladder](02-async-library/)

**A library with no I/O dependencies**, tested with no dependencies either — stdlib only.
A webhook delivery client with retry, jitter, and a TTL cache.

Shows: the three tiers of [`spec/08`](../spec/08-patching-and-isolation.md) side by side in one
suite —

- **tier (a)** dependency injection, fully concurrent, and what you get when a seam already exists
  (the cache's clock is a constructor argument, so no patching is needed at all);
- **tier (b)** stock `unittest.mock`, which velox detects and schedules **solo** — including what
  that costs, printed in the run summary;
- the decorator form velox finds for free vs. the `with mock.patch(...)` form it cannot see
  statically and that you (or `velox migrate`) must mark.

Also: `velox.log_records`, `velox.raises`, `velox.approx`, `@velox.timeout`, `velox.tmp_path`, and a
sync test running on the executor.

The point: **most mocking should not be mocking.** The example is arranged so the reader can see the
cost of the difference in one summary line.

Standalone — no `requirements.txt`.

### [03 — Shared resources, exclusion, and the safety net](03-shared-resources/)

**What concurrency actually breaks, and the primitives for it.** A ledger over stdlib `sqlite3`,
plus a webhook receiver that binds a fixed port.

Shows: `exclusive=True` and `exclusive="token"`, a test whose footprint is *two* tokens (admitted
atomically — see [`spec/06 §2`](../spec/06-scheduling-and-determinism.md)), `@velox.solo` for a
process-global mutation, and the loop-starvation watchdog catching a blocking `sqlite3` call that
was never wrapped in `asyncio.to_thread`.

The point: **resource conflicts are declared on the resource, not worked around in the scheduler.**
Everything that does not conflict runs 32-wide; the three things that do conflict say so in one
keyword argument.

Standalone — no `requirements.txt`.

---

## Reading order

If you are reviewing the API, read them in order: 01 establishes the shape, 02 stresses the parts
that are contentious (mocking, solo cost), 03 covers the parts that only exist because velox is
concurrent.

If you are evaluating whether to adopt, read 01 and then the "What this costs you" section of 03.

## Running them (once velox exists)

```bash
cd examples/01-fastapi-crud
uv venv && uv pip install -r requirements.txt -e ../..
velox
```

Every example's README lists its own commands and a sketch of the expected output.

---

## Notes for the spec review

Writing these surfaced three things the spec does not currently answer.

1. **How does `tests/test_users.py` import `tests/fixtures.py`?** [`spec/02 §5`](../spec/02-cli-and-config.md)
   says velox never touches `sys.path`, and [`spec/03`](../spec/03-discovery-and-collection.md)
   imports test modules under generated `velox_tests.*` names. Neither makes the *test tree* an
   importable package, so a shared fixture module cannot be imported by a test module. These
   examples assume velox prepends the **rootdir** to `sys.path` exactly once at startup — one
   predictable insertion, not pytest's per-conftest-directory games. It needs to be written down
   either way.
2. **`velox.tmp_path_factory` has no named type.** Used here as `velox.TmpPathFactory` with a
   `.mktemp(name)` method, matching pytest.
3. **`spec/01 §3`'s `api_client` example mutates a module-level `app`.** That is the exact footgun
   [`spec/08 §3`](../spec/08-patching-and-isolation.md) warns about — `dependency_overrides` is
   per-app-instance state, so concurrent tests sharing one app overwrite each other. Example 01 uses
   an app factory instead; the spec snippet should probably follow.
4. **`Fixture.with_()` written inline inside `Depends(...)` trips ruff's B008**, and unlike
   `Depends` itself there is no qualified name to add to `extend-immutable-calls` — it is a method
   on an arbitrary object. So the examples bind derived fixtures to module-level names
   (`flaky_relay = relay.with_(transport=flaky_transport)`), which reads better and is shareable
   anyway. Worth being the documented idiom rather than something every adopter rediscovers.

   Every example's `pyproject.toml` also carries
   `extend-immutable-calls = ["velox.Depends"]`; without it B008 fires on every test in the suite.
   That line belongs in the getting-started docs.
