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

- [x] **Dogfood `examples/01-fastapi-crud`, locally** — its own `.venv`
  (`uv venv && uv pip install -r requirements.txt -e ../..`), run repeatedly under
  `uv run velox`/`.venv/bin/velox` at `--concurrency 1`, 16 (config default), and 64: 22 tests, 0
  failed, 0 collection errors, every time. Three real bugs found and fixed, all in the example
  (not velox itself) — see the commit for detail on each:
  - `tests/fixtures.py::engine` — pysqlite/aiosqlite's own implicit-transaction heuristics
    defeated the transaction-per-test rollback `session` relies on, so a row one test inserted
    stayed visible to the next (`alice@example.com` UNIQUE-constraint `IntegrityError`, reproduced
    at `--concurrency 1` too — not a concurrency bug). Fixed with SQLAlchemy's own documented
    `connect`/`begin` event-listener recipe for SQLite.
  - `app/main.py::create_user` never logged the "email already registered" case its own test
    asserted against, and never rolled back the session after the caught `IntegrityError` either
    — a latent `PendingRollbackError` for any later operation sharing that session (only reachable
    through the test client, which shares one session across a test's requests; production hands
    each request its own). Added both the `logger.warning(...)` and the `session.rollback()`.
  - `tests/test_orders.py::test_duplicate_email_is_logged` read `r.message` off a raw
    `velox.log_records` `LogRecord`, which is unset (velox's capture handler never calls
    `record.getMessage()`/formats onto a stream the way pytest's `LogCaptureHandler` does —
    deliberately no pytest runtime-compat shim, per the project's compat-via-codegen-only
    decision). Fixed to read `logs.messages` instead; `examples/02-async-library/tests/
    test_delivery.py:87` has the identical bug, still open (see the CI item below).

  Also surfaced four things in velox's own public API that the example (written ahead of the
  runner) assumed worked: `@velox.parametrize` expansion, `class Test*` grouping, `@velox.xfail`
  execution, and `-m`/tag selection plus most of spec/02 §1's CLI surface. None block "velox runs
  its own suite" or this example's green run (routed around all four — see its README's "Known
  gaps"); broken out into their own tracked items below rather than fixed here, since each is its
  own spec/00-§7-scale feature, not a slice-sized bug fix. — `3785c36`
- [ ] **...in CI, and `examples/02-async-library` too** — `examples/02-async-library` is
  stdlib-only (no separate env needed) but still has the "4 unrelated pre-existing bugs" the
  rootdir-import-convention slice surfaced and left open (see "Already shipped" above), plus the
  `r.message`/`logs.messages` bug named just above — triage the same way example 01's did before
  it's worth calling green. Once both examples run clean, add them as a CI job. This — plus the
  two example dogfood passes above — is the concrete, checkable form of "velox runs its own
  suite": the closest thing to a self-hosted proof available before the migration codegen (M3)
  makes running `tests/` itself under velox realistic.

## Tracked but not blocking the gate

- [ ] **Test-id selection** (`path.py::test_name`) — `cli.py:159` (`_invalid_path_argument`)
  explicitly rejects `::` today with "not implemented yet (M0)". Documented as supported
  invocation syntax in spec/02 §1 but not part of M1's DI/concurrency/assertions/capture/reporter
  bullet, and nothing above depends on it. Pick up opportunistically or fold into whichever M2 CLI
  slice touches `-k`/`-m` selection.
- [ ] **`@velox.parametrize` expansion** — `ParamSet` is recorded on a function's marks
  (`_marks.py`) the moment the decorator is applied, but `_collect.collect` never reads it: a
  parametrized test collects as a single record with its extra parameter treated as a missing
  `Depends()` injection, which `_fixtures.plan_for` rejects with a `DIError` (`parameter(s) ...
  have no default and are not injected via Depends(...)`) — a collection error, not a graceful
  "not implemented" message, and not the "multiple passing tests" spec/01 §9 documents as MVP.
  Found dogfooding `examples/01-fastapi-crud` (see above); worked around there by writing the
  parametrized cases out as separate functions.
- [ ] **`class Test*` grouping** — spec'd as pure namespacing (spec/01 §7: no `__init__`, `self`
  ignored, ids read `path.py::TestFoo::test_bar`), and listed MVP there and in spec/03 §8. Not
  implemented: `_collect.collect` only looks for module-level `async def test_*` (`vars(module)
  .values()`), so a `Test*` class's methods are silently collected as zero tests — no
  `CollectionError`, no `Skipped` entry, nothing. Worth at minimum a loud diagnostic (I8: "silent
  passes are bugs" — this is a silent *absence*, arguably the same class of problem) even before
  the feature itself lands. Found dogfooding `examples/01-fastapi-crud`; worked around there by
  flattening the one `Test*` class to free functions.
- [ ] **`@velox.xfail` execution** — `XFail` is recorded on a function's marks the same way `skip`/
  `skipif` are, but `_run.py`'s `Outcome` enum has four members today (`PASSED`/`FAILED`/`ERROR`/
  `TIMEOUT` — its own module docstring says so explicitly) and nothing reads `marks.xfail` to turn
  a failing call into `XFAILED` (or a passing one into `XPASSED`, under `strict=True`). A test
  decorated `@velox.xfail(..., strict=True)` today just reports `FAILED`, indistinguishable from a
  real regression. Needs the `Outcome` enum extended (spec/05 §4) plus reporter/exit-code changes
  to match, not a small patch. Found dogfooding `examples/01-fastapi-crud`; worked around there
  with `skip` (which *is* wired end to end) instead.
- [ ] **Tag-based selection (`-m`) and the rest of the CLI surface** — `@velox.tag` records names on
  a function's marks (works, and is harmless to apply today) but nothing consumes them: `-m`
  doesn't exist in `cli.py`'s parser, alongside `-k`, `-v`/`-q`, `--serial`, `-x`, `--durations`,
  and `--collect-only` — all documented in spec/02 §1, all absent from `build_parser`. Matches
  spec/00 §7's MVP table (`-k`/`-m` explicitly "Deferred"); grouped here as one item since they're
  naturally one CLI slice's worth of work. Bare paths (a file or a directory, no `::`) already
  work today and aren't part of this item.
