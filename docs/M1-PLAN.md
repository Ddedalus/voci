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
- [x] **Dogfood `examples/02-async-library`, locally** — standalone (`uv venv && uv pip install
  -e ../..`, no separate `requirements.txt`), run repeatedly at `--concurrency 1`/16 (config
  default)/64: 28 tests, 0 failed, 4 skipped, 0 collection errors, every time — this closes the "4
  unrelated pre-existing bugs" the rootdir-import-convention slice surfaced and left open (see
  "Already shipped" above), plus the `r.message`/`logs.messages` bug example 01 also hit. Two real
  bugs found and fixed:
  - `relay/settings.py::Settings.from_env` read `cls.endpoint`/`cls.retries`/`cls.cache_ttl` as
    its fallback default — but `Settings` is `@dataclass(slots=True)`, which replaces each of
    those class attributes with a slot descriptor, not the field's actual default value, so any
    key missing from the passed-in mapping got a `member_descriptor` object instead of a real
    default, `TypeError`ing the moment `int()`/`float()` touched it. Fixed by building
    `defaults = cls()` (a real instance, real values) and reading the fallback off that instead.
  - `tests/test_delivery.py::test_retries_are_logged` had the same `r.message` bug example 01's
    `test_duplicate_email_is_logged` did; fixed the same way (`logs.messages`).

  This example's whole subject is the solo/mock-patching-detection tier (spec/08), which doesn't
  exist yet any more than example 01's four gaps did — and here the gap is more than "undetected,
  unscheduled": `_fixtures.plan_for` reads `func.__code__`/`func.__defaults__` directly (never
  `inspect.signature`/`__wrapped__`), so a `@mock.patch(...)`-decorated test presents as
  `(*args, **keywargs)` — zero named parameters found, so any `Depends(...)` default on the real
  function underneath is *silently never resolved* rather than rejected; the parameter gets the
  raw, unresolved `Depends(...)` object instead of a fixture value or a collection error. Worse,
  and verified directly rather than just reasoned about: a `mock.patch` on a module-global name
  patches it for every concurrently-running test that reaches the same code path, not only for
  tests that patch it themselves — a standalone `asyncio.TaskGroup` repro running this example's
  decorator-based patch test alongside an unrelated tier-(a) test (no patching of its own) got a
  corrupted `call_count` on the very first trial. Both marks that would fix this
  (`@velox.solo`/`@mock.patch` detection) are declared, not enforced (`_run.py`'s own module
  docstring: "the solo write-lock tier... still deliberately not built"), so the four tests that
  would demonstrate it live are `@velox.skip`, not run unguarded — see the example's own README
  "Known gaps" for the full reasoning and the exact repro for each. — `96dfc9b`
- [x] **Dogfood `examples/03-shared-resources`, locally** — standalone, same pattern, run
  repeatedly at `--concurrency 1`/32 (config default)/64: 23 tests, 0 failed, 1 skipped, 0
  collection errors. `pyproject.toml` set `watchdog_threshold`/`basetemp_retention`, both real
  spec/02 §3 keys the config loader correctly rejects as unknown (no consumer before M2) — removed
  from the file, documented instead. Two real bugs found and fixed, both in the example:
  - `tests/fixtures.py::migrated_store`/`migration_db` opened a `sqlite3.Connection` via
    `asyncio.to_thread(sqlite3.connect, ...)` and then used it across further `to_thread` calls —
    but `asyncio.to_thread` hands each call to whichever worker the default executor's pool has
    free, not the same one from call to call, and a plain `sqlite3.connect` refuses to touch its
    connection from any thread but the one that created it: `sqlite3.ProgrammingError: SQLite
    objects created in a thread can only be used in that same thread`, on nearly every test. Fixed
    with `check_same_thread=False` — safe here specifically because the awaits already fully
    serialize access to the connection; nothing touches it from two threads *at once*.
  - `ledger/store.py::Store.connect` re-issues `PRAGMA journal_mode=WAL` as the first statement on
    every brand-new connection, every call, from every test — switching journal mode needs a real
    (if brief) exclusive lock, so at high concurrency (`--concurrency 64`) dozens of first-ever
    calls raced for that lock before any of them had switched the database over yet:
    `sqlite3.OperationalError: database is locked`, intermittent (2 failures in the first ~85
    stress runs before the fix, 0 in 250 after). Fixed by switching to WAL once, in the
    session-scoped `migrated_store` fixture, before any test-level connection is ever opened —
    every later `PRAGMA journal_mode=WAL` is then already-WAL and a no-op.

  Also, like example 02: this example's whole subject is scheduling machinery — `exclusive=`
  admission, `@velox.solo`, and the loop-starvation watchdog — none of which exists (`_run.py`'s
  docstring again; the watchdog has no code at all, not even declared-but-unenforced). Two real,
  verified conflicts followed directly from that: `test_webhooks.py`'s `receiver` fixture binds a
  real, fixed OS port, and running its four original tests concurrently reproduced a real `OSError:
  address already in use` deterministically, every time — merged into one test that runs all four
  scenarios in sequence against a single bind instead of restoring the four-way split. Two
  `@velox.solo`-marked tests flip the same process-global flag registry `test_ledger.py::
  test_transfer_is_permitted_to_overdraw_by_default` depends on staying off; verified with the same
  adversarial-`TaskGroup` technique as example 02 (3000/3000 trials corrupted) that one of the two
  actually collides with that test, so it's `@velox.skip`, while the other (which flips a flag
  nothing else reads) stays live. The blocking-call watchdog-bait test still runs and still passes
  — real stall, no diagnostic, nothing else in this small suite times out waiting for the loop back
  — its docstring and `@velox.tag("watchdog-demo")` were rewritten to stop promising output that
  can't appear. Also corrected: a test claiming to demonstrate "returning a value"/un-awaited-
  coroutine detection (I8) that neither its own body nor `_run.py` actually implements — see the
  new tracked item below. — `96dfc9b`
- [ ] **...in CI** — all three examples now run green, repeatedly, locally. Add them as a CI job.
  This — plus the three example dogfood passes above — is the concrete, checkable form of "velox
  runs its own suite": the closest thing to a self-hosted proof available before the migration
  codegen (M3) makes running `tests/` itself under velox realistic.

## Tracked but not blocking the gate

- [ ] **Test-id selection** (`path.py::test_name`) — `cli.py:159` (`_invalid_path_argument`)
  explicitly rejects `::` today with "not implemented yet (M0)". Documented as supported
  invocation syntax in spec/02 §1 but not part of M1's DI/concurrency/assertions/capture/reporter
  bullet, and nothing above depends on it. Pick up opportunistically or fold into whichever M2 CLI
  slice touches `-k`/`-m` selection.
- [x] **`@velox.parametrize` expansion** — `ParamSet` was recorded on a function's marks
  (`_marks.py`) the moment the decorator was applied, but `_collect.collect` never read it: a
  parametrized test collected as a single record with its extra parameter treated as a missing
  `Depends()` injection, which `_fixtures.plan_for` rejected with a `DIError` (`parameter(s) ...
  have no default and are not injected via Depends(...)`) — a collection error, not a graceful
  "not implemented" message, and not the "multiple passing tests" spec/01 §9 documents as MVP.
  Found dogfooding `examples/01-fastapi-crud` (see above); worked around there by writing the
  parametrized cases out as separate functions. Fixed by a new `_collection/parametrize.py`
  (`known_params_of`/`cases_for`, the cartesian-product and id-generation logic) and threading
  `known_params` through `_fixtures.plan_for`/`_check_missing_injections` so a parametrized
  argument stops reading as a missing injection; `collect.py` now expands one `TestRecord` per
  case, all sharing the one plan built for the function, and `_run.py` merges each case's
  `params` into its call kwargs. — `8d0a432`, review `81604e8`
- [ ] **`class Test*` grouping** — spec'd as pure namespacing (spec/01 §7: no `__init__`, `self`
  ignored, ids read `path.py::TestFoo::test_bar`), and listed MVP there and in spec/03 §8. Not
  implemented: `_collect.collect` only looks for module-level `async def test_*` (`vars(module)
  .values()`), so a `Test*` class's methods are silently collected as zero tests — no
  `CollectionError`, no `Skipped` entry, nothing. Worth at minimum a loud diagnostic (I8: "silent
  passes are bugs" — this is a silent *absence*, arguably the same class of problem) even before
  the feature itself lands. Found dogfooding `examples/01-fastapi-crud`; worked around there by
  flattening the one `Test*` class to free functions.
- [x] **`@velox.xfail` execution** — was recorded on a function's marks the same way `skip`/
  `skipif` are, but nothing read `marks.xfail` to turn a failing call into `XFAILED` (or a passing
  one into `XPASSED`, under `strict=True`); a `strict=True` mark just reported `FAILED`,
  indistinguishable from a real regression. Found dogfooding `examples/01-fastapi-crud`; worked
  around there with `skip` (which *was* wired end to end) instead. Fixed by extending `_run.py`'s
  `Outcome` enum with `XFAILED`/`XPASSED` and reading `marks.xfail` in `_resolve_call_outcome`;
  `_report.py` and `cli.py`'s summary line updated to match. `@velox.timeout(...)` was fixed the
  same pass — `dispatch_one` now reads `marks_of(record.func).timeout` and uses it in place of the
  suite-wide `--timeout` for that one test.
- [ ] **Tag-based selection (`-m`) and the rest of the CLI surface** — `@velox.tag` records names on
  a function's marks (works, and is harmless to apply today) but nothing consumes them: `-m`
  doesn't exist in `cli.py`'s parser, alongside `-k`, `-v`/`-q`, `--serial`, `-x`, `--durations`,
  and `--collect-only` — all documented in spec/02 §1, all absent from `build_parser`. Matches
  spec/00 §7's MVP table (`-k`/`-m` explicitly "Deferred"); grouped here as one item since they're
  naturally one CLI slice's worth of work. Bare paths (a file or a directory, no `::`) already
  work today and aren't part of this item.
- [x] **`exclusive=`/`@velox.solo` admission, and `@velox.isolated`'s subprocess tier** — all three
  were recorded on a function's/fixture's marks (`Marks.solo`, `Marks.isolated`, `Fixture.exclusive`)
  with nothing acting on them: a test carrying any of the three ran exactly like one that doesn't —
  fully concurrent, no suite-wide lock, no subprocess. Where the marked resource is genuinely shared
  and mutable, this wasn't just unfinished, it was actively unsafe: found dogfooding
  `examples/02-async-library` (two `mock.patch` calls on the same module-global target, verified
  colliding 2000/2000 adversarial trials) and `examples/03-shared-resources` (a fixed TCP port,
  `OSError: address already in use`, reproduced deterministically every run; a shared feature flag
  registry, verified colliding 3000/3000 adversarial trials). `exclusive=`/`solo` were fixed first,
  by `AdmissionGate` (`_run.run_suite`): concurrency, `exclusive=` token contention, and `solo` are
  one admission decision, not three layered gates (`docs/rationale.md`). `@velox.isolated` was
  fixed by `_run/isolated.py`: a marked test is still admitted through the same gate, then
  dispatched to a fresh `python -m velox._run._isolated_worker` subprocess, which re-collects the
  one file naming it and runs it through this package's own `run_suite` a second time, on that
  process's own loop; the result crosses back as JSON.
- [x] **`@mock.patch`-decorated tests silently skipped DI, not just solo scheduling** — a sharper
  version of the item above, found dogfooding `examples/02-async-library`:
  `_fixtures.plan_for`/`plan_of` read `func.__code__`/`func.__defaults__` directly, so a test
  wrapped by `@mock.patch(...)` — whose real signature is `(*args, **keywargs)` — presented zero
  named parameters to collection. Any `Depends(...)` default on the *real* function underneath was
  never found, so it was never resolved either: `_run.py` called the wrapper with no arguments and
  the parameter that should have been injected got Python's own fallback, the literal, unresolved
  `Depends(...)` object — an I8 violation on its own. Fixed with the spec/08 tier-(b) work in one
  pass (`velox/_mocking.py`): collection reads the plan and the definition line off
  `real_function(func)` while `TestRecord.func` stays the wrapper, and `plan_for` is told how many
  leading positional parameters `unittest.mock` fills so they don't read as missing injections (a
  `Depends()` declared in one of those slots is now a collection error). Detection lands on
  `TestRecord.patches` — `patchings` for `mock.patch`, the wrapper's closure for `mock.patch.dict`,
  which records no `patchings` — and `dispatch_one` takes the gate solo for it; `_report.terminal`
  prints the drained wall clock. The context-manager form is caught by a guard on
  `unittest.mock._patch.__enter__`/`_patch_dict.__enter__`, installed for the run only when the
  suite imported `unittest.mock` at all, which raises before the patch is written unless the test
  is solo or isolated. `examples/02-async-library`'s three `@velox.skip`ped patching tests (and its
  `@velox.isolated` `chdir` one) run for real now: 34 tests, 33 passed, 1 skipped, repeated at
  `--concurrency 1`/16/64.
- [ ] **The loop-starvation watchdog does not exist** — no code at all, not even a declared,
  unenforced mark; `watchdog_threshold` is a real spec/02 §3 config key the loader correctly
  rejects as unknown (M1-PLAN.md's own config-loader entry above: "no consumer before M2"), and
  `--watchdog-threshold`/`--watchdog fail` aren't in `cli.py`. A blocking sync call from a
  coroutine (`examples/03-shared-resources/ledger/service.py::LedgerService.balance_blocking`)
  still stalls the real event loop for real today — the bug the watchdog would catch is live,
  nothing diagnoses it.
- [ ] **A test returning a value, or leaving a coroutine un-awaited, isn't caught.** I8 ("silent
  passes are bugs") names this failure shape explicitly as unacceptable, but nothing in `_run.py`
  inspects a dispatched test's return value, and no warnings filter escalates a `RuntimeWarning:
  coroutine ... was never awaited` to a failure. Found dogfooding `examples/03-shared-resources`,
  whose `test_a_test_must_not_return_a_value` claimed to demonstrate this without actually
  exercising either case in its body; renamed to `test_awaiting_actually_runs_the_coroutine` and
  its docstring corrected to say so, rather than deleted, since the underlying gap is real and
  worth a test once it exists to test.
