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
- [x] **`[tool.velox]` config loader** — `_config.resolve` (upward search for the `pyproject.toml`
  declaring `[tool.velox]`, stopping at the git root) plus `cli.main` merging it against the CLI
  at `CLI > [tool.velox] > built-in default`. Recognizes `testpaths`, `concurrency`, `timeout`,
  `test_file_patterns`, `ignore`, `env` — the keys the shipped examples actually use;
  `watchdog_threshold` deliberately deferred (no consumer before M2). — `6674f2d`, review
  (8-angle `/code-review high`, run twice after a mid-review interruption) found and fixed: a
  `test_file_patterns = []`/`ignore = []`-style truthiness bug, an `os.environ` mutation that
  could leak on a `_rewrite.install` exception, unvalidated `testpaths` entries silently producing
  "0 tests" instead of a usage error, and config-sourced bad values being misattributed to
  `--concurrency`/`--timeout` in error messages — fixed in `864ab6e`.
- [x] **Rootdir import convention** — resolves Outstanding Decision #1 (spec/00 §11): `cli.main`
  prepends `rootdir` to `sys.path[0]` exactly once, before the first test module import, removed
  again on the way out (same "repeated in-process `main()` calls must not leak" shape as the
  config-loader's `env_backup`). Makes plain absolute imports rooted at `rootdir` resolve via
  PEP 420 namespace-package lookup (`from tests.fixtures import api_client`, `from relay.cache
  import FakeClock`) without an `__init__.py` anywhere; deliberately does *not* make relative
  imports between test modules work (`from .conftest import x` — those resolve against the
  synthetic `velox_tests.*` package name, unsupported by design). Spec updated:
  [00](../spec/00-overview.md) §11, [02](../spec/02-cli-and-config.md) §5,
  [03](../spec/03-discovery-and-collection.md) §3. Built on a separate branch
  (`rootdir-import-convention`, `331c477`) concurrently with the config-loader review pass above,
  merged into `main` at `c380eb6` (one manual conflict in `cli.py` where both branches touched the
  same region — resolved by keeping the config-loader's env-leak-fix structure and slotting the
  `sys.path` insertion in beside it on the same reasoning). Verified against
  `examples/02-async-library`: the `ModuleNotFoundError: No module named 'relay'` collection
  failure is gone (surfacing 4 unrelated pre-existing bugs in that example, out of scope here —
  the "dogfood the examples" item below).

Both config-loader and rootdir-import-convention were needed before the example suites could even
*collect* — config-loader alone got their `concurrency`/`timeout`/`env` applied but not their
imports resolving; rootdir-import-convention alone would have had no config-driven `testpaths`/
`env` to run with. Together, that's every named M1 component plus what blocked the gate.

## Remaining work

- [ ] **Dogfood the example suites, in CI** — get
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
