# M1 implementation plan — core runner

Scope, verbatim from [spec/00-overview.md](../spec/00-overview.md) §8:

> **M1 — Core runner.** DI + scopes + teardown; concurrency + semaphore; vendored assertions;
> capture; the reporter. This is v0.1 and the bulk of the work.
>
> **Gate on M1:** velox runs its own suite.

Each entry below is sized for one Opus coding-agent session: one slice, one PR, one (or a small
paired set of) commits, reviewed with `/code-review` before merge — that has been the working
pattern for every item already shipped.

**For coding agents:** when you finish an item, tick its box and append a one-line pointer to the
commit(s) that closed it (`— a1b2c3d`). Don't rewrite or delete history here; if scope changes
under you, add a note rather than editing the original bullet. Add new items under "Remaining
work" if you discover something this plan missed — don't silently fold it into an existing item.

## Already shipped

Verified against `git log` and the current tree on 2026-08-09 — these are done, reviewed, and
covered by the 408-test suite (`just test`), not aspirational.

- [x] **Assertion introspection** — vendor pytest's `rewrite.py` (3 globals → ContextVars), PEP 657
  fallback, comparison-diff engine (spec/07). — `1c374f6`
- [x] **M0 walking skeleton** — discovery, importlib-only import, one async test run, pass/fail,
  exit codes (spec/02, spec/03). — `f37a446`, review `d71e91d`/`fbf51d4`
- [x] **Dependency injection** — graph from `__defaults__`, `ResolutionPlan` per test, static
  validation (cycles, scope compatibility, bad `Depends()`), four scopes (`call`/`function`/
  `module`/`session` — a superset of the `function`/`session` MVP floor in spec/00 §7),
  single-flight futures with exception caching, refcount teardown with `ExceptionGroup`
  collection, `AsyncExitStack`-driven async generators (spec/04). — `bc9d3ac`, review
  `18e396c`/`de8c70d`
- [x] **Concurrency** — tests dispatched as `asyncio.Task`s under a shared `asyncio.Semaphore(N)`,
  per-test `asyncio.timeout`, `--concurrency`/`--timeout` wired end-to-end, `concurrency=1`
  degenerates to exact serial (spec/05, spec/06 §7). — `e09e27a`, review
  `6dbb8ee`/`42e5475`/`15f6348`
- [x] **Capture** — stdout/stderr routed through per-test `Sink`s, root logging handler with
  structured record retention, `-s`/`--capture=no` with id-prefixed live passthrough, `tmp_path`/
  `tmp_path_factory`, `--basetemp` (spec/09). — `e7037ff`, review `5088f8b`/`7530b5e`
- [x] **Reporter** — jest-style per-file tty blocks, failure details and short summary in logical
  order, non-tty mode, wall-vs-Σ final line (spec/10). — `f5a689c`, review `4c581c0`/`9b045f6`

That's all five named components. What's left is closing the **gate**, not the component list.

## Remaining work

The M1 gate ("velox runs its own suite") is not yet demonstrated. Two of the example suites
under `examples/` — which are the actual "real suite" dogfood targets (spec/00 §7 intro: "a runner
good enough to run velox's own test suite and one real FastAPI service suite") — currently fail to
collect under `uv run velox`, and neither is wired into CI. Root-caused below; each is a real,
scoped gap, not flakiness.

- [ ] **Rootdir import convention** — resolve Outstanding Decision #1 (spec/00 §11) and implement
  it. `_collect.py` currently does *no* `sys.path` insertion by design (`_collect.py:295`), so any
  test module that imports a sibling — `examples/02-async-library/tests/test_cache.py: from
  relay.cache import FakeClock`, or `tests/assertion/test_explanations.py: from .conftest import
  callequal` — fails to collect with `ModuleNotFoundError`. This is the actual root cause of both
  failures reproduced below, and it blocks essentially every non-trivial suite (shared
  `tests/fixtures.py`, the package under test itself), so it's the highest-priority item here.
  Needs: a written resolution (spec/00 §11 point 1 already has the user's steer — "prepend the
  rootdir to sys.path once at startup" — someone needs to turn that into an implementation and a
  spec/03 update), then the `_collect.py` change, then tests.
  - Repro: `cd examples/02-async-library && uv run --with-editable ../.. velox .` →
    `ModuleNotFoundError: No module named 'relay'`.
  - Repro: `uv run velox tests/` → `tests/assertion/test_explanations.py` fails collection on
    `from .conftest import callequal, callop` (`ModuleNotFoundError: No module named
    'velox_tests'`).

- [ ] **`[tool.velox]` config loader** — `cli.py:144` notes "velox has no `[tool.velox]` loader
  yet — nothing in this package reads `pyproject.toml`". `examples/01-fastapi-crud/pyproject.toml`
  already carries `testpaths`, `concurrency`, `timeout`, and `env` under `[tool.velox]`
  (spec/02 §3) expecting it to be honored; right now it's silently ignored. This blocks the
  FastAPI example from getting its `ENVIRONMENT`/`SIGNUP_BONUS_CENTS` env vars set at all, on top
  of the import problem above. Scope this to the keys the shipped examples actually use rather
  than the full spec/02 §3 surface — the rest can stay roadmap.

- [ ] **Dogfood the example suites, in CI** — once the two items above land, get
  `examples/01-fastapi-crud` (needs its own `uv sync` — it depends on `fastapi`/`sqlalchemy`, not
  present in the root env) and `examples/02-async-library` (stdlib-only, no separate env needed)
  actually green under `uv run velox`, fix whatever real bugs that surfaces, and add both as a CI
  job. This is the concrete, checkable form of "velox runs its own suite" — the closest thing to a
  self-hosted proof available before the migration codegen (M3) makes running `tests/` itself
  under velox realistic.

## Tracked but not blocking the gate

- [ ] **Test-id selection** (`path.py::test_name`) — `cli.py:159` (`_invalid_path_argument`)
  explicitly rejects `::` today with "not implemented yet (M0)". Documented as supported
  invocation syntax in spec/02 §1 but not part of M1's DI/concurrency/assertions/capture/reporter
  bullet, and nothing above depends on it. Pick up opportunistically or fold into whichever M2 CLI
  slice touches `-k`/`-m` selection.
