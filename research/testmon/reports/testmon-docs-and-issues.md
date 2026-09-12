# pytest-testmon's documented failure modes, from testmon.org and its GitHub issues

> Subagent report from the 2026-09-12 research pass behind `plans/testmon-plan.md`, kept verbatim. Paths under `/tmp` were rewritten to their copies in `research/testmon/`; the venvs they ran in weren't kept (see `../README.md`).

# pytest-testmon: documented failure modes, limitations, and design reasoning

Sources used: testmon.org homepage and blog posts (URLs inline below), and GitHub issues on `tarpas/pytest-testmon` (viewed via `gh issue view`). The local checkout at `/home/hubert/voci/oss/pytest-testmon` has no doc content beyond a README pointing to testmon.org — confirmed by reading it.

## 1. Core mechanism: coverage.py + block checksums

**Source:** https://testmon.org/blog/determining-affected-tests/, https://testmon.org

- testmon runs **coverage.py** internally during the recording run to capture which lines each test executes.
- It doesn't store executed line numbers directly; it splits each source file into **blocks** (line ranges) with "holes denoted by a placeholder," and stores a **checksum per block**, not the code. A test depends on the blocks coverage showed it reached.
- Stated logical premises: "an unexecuted line of code doesn't influence the outcome of execution of the surrounding code," and "a method body which is not reached/executed by a specific test cannot influence the outcome of that test."
- Their own framing of reliability: **"The limits and reliability of this method are pretty much the same as the limits of coverage.py."** They explicitly punt failure-mode enumeration to coverage.py's own docs rather than owning a list.
- Granularity evolved from whole-file/module (original) to **per-method** — GitHub issue #7, "per-method granularity" (closed, implemented): the motivator was that pytest itself has "a lot of functionality split into only a couple of files," so file-granularity re-ran everything in a file on any edit, including editing an unrelated test. They noted the caveat themselves in that issue: "in Python you can break any execution unit without changing any of its lines, by simply adding statements after the block" — i.e., block-level checksums are not perfectly sound against structural edits either.
- Database schema/version churns between releases (v1.2 bumped schema to v8, incompatible with prior; v2.0 changed data format again) — old `.testmondata` files are silently discarded ("new DB") on a version bump rather than migrated.

**Reasoning:** checksums-not-full-code keeps the DB small and stable against unrelated whitespace/formatting elsewhere in the file, while still invalidating precisely on real content change to a block.

**Admitted gap:** whitespace/comments *were* counted as changes as of v1.0 ("Whitespace and comments are taken into account when detecting changes... we hope to improve in future versions" — https://testmon.org/blog/new-in-testmon-100/), an explicit example of an over-approximation they knew about and left unfixed for a release.

## 2. Module-level / global-state code (no function call involved)

**Issue:** https://github.com/tarpas/pytest-testmon/issues/191 — "Changes to global / class variables are ignored (if no method of their module is executed)"

- Reproduced case: a module-level variable or a class attribute (`FOO = "bar"`) that a test reads directly, with **no function/method call** into that module, is invisible to testmon. Changing or even deleting the attribute does not trigger a re-run.
- Maintainer's own words: **"Unfortunately this is quite a fundamental limitation. It's not impossible to fix but hard."**
- Workaround noted in the thread: if *any* function/method from that module is called by the test, coverage.py's line tracing does pick up the module body (since module-level code executes once at import and coverage attributes those lines), so the miss is specific to "imported an attribute and used it directly with zero calls into the defining module." Still open as of testmon 2.0.9 per the last comment.

This is the same failure family voci's plan calls "import-time and module-level code" (Risk #2), but testmon's version is narrower and sharper: it's not just "first importer only" — it's "never traced at all if the test makes no call into the module."

## 3. Data files / non-Python dependencies

**Issues:** https://github.com/tarpas/pytest-testmon/issues/178, https://github.com/tarpas/pytest-testmon/issues/12

- Direct maintainer answer to "if my code does `open('path/to/file.json').read()`, would testmon flag that file?": **"No, testmon wouldn't be able to recognize `file.json` as a dependency of the code... coverage is and will always deal with executable files and lines but not I/O."**
- They looked at coverage.py's **file tracers plugin API** as a possible extension point but judged it a mismatch: file tracers are meant for "yml file with instructions or django templates," not generic I/O-read data files.
- Issue #12 proposed an explicit dependency-declaration mechanism (`@testmon.depends_on('path')` decorator, or a "mark test dirty" / "is dirty" programmatic hook) as the only real fix. **Never shipped** — issue is still open, no code, years old. Their conclusion in effect: this class of dependency needs a declared association or it's a hole, full stop — same as voci's plan states independently.
- A related open feature request (#12 comment) is a subprocess variant: "1-1 mapping between module/file and test, but Coverage can't trace it since the module is executed as a subprocess" — ties into the subprocess gap below.

## 4. Environment changes: installed packages

**Blog:** https://testmon.org/blog/version-11-is-out/ ; **Issues:** #183, #216, #255, #245, #249

- v1.1 added installed-package tracking: **"With version 1.1 comes the ability to detect changes of packages(libraries)."** Before that, testmon tracked only "code inside tested project and configured environment variables." On upgrade to 1.1, users had to `pip install --upgrade` and **manually delete `.testmondata`** — no forward migration.
- Mechanism: on each run testmon reads current installed package name+version list (`testmon/common.py`) and compares it to what's stored in the DB's `environment` table; any diff forces a full run with the message *"The packages installed in your Python environment have been changed. All tests have to be re-executed."*
- **Issue #183** ("Testmon very sensitive towards library changes"): installing/upgrading *any* library — including ones unrelated to the module under test — invalidates the whole suite, because the check is suite-wide, not per-dependency-used. Maintainer's fix: a beta that **ignores version changes below the 3rd component** (patch version), plus an **undocumented `testmon_ignore_dependencies`** config option to exclude specific noisy packages by name. Maintainer's own framing: "We'll drop the sensitivity in the future quite a bit more" — treated as inherent overkill they're walking back, not a settled design.
- **Issue #255 / #245** (real bugs, not just design tension): the DB query fetching the "last known package list" for comparison **wasn't ordered by row id**, so after the first package-change event it always compared against the *first-ever* recorded package snapshot instead of the most recent one — meaning **every single run after any one package change re-triggered "packages changed," forever**. Fixed in v2.1.4. A related bug (#245) is that writing the new package-list row required a **write transaction** but the DB is opened read-only under xdist workers, causing a hard crash (`sqlite3.OperationalError`) instead of a graceful full-run fallback, when a new dependency appeared mid-xdist-run.
- **Issue #249**: user asked for an option to key "packages changed" off a lockfile hash (poetry.lock) instead of enumerating installed packages, specifically because the naive comparison was so unreliable in CI/cross-machine setups. Maintainer response: reluctant to add another flag, prefers to fix the underlying comparison bug instead — i.e., they see config-surface growth as itself a cost.
- **Issue #216**: reports of testmon becoming "more sensitive" to differently-packaged environments across versions when moving a `.testmondata` between machines with nominally the same lockfile-resolved versions but different build hashes/wheels — largely traced back to the same ordering bug and to genuinely different package metadata across machines (Spark cluster wheel installs).

**Config surface for environments:** `environment_expression` (renamed from `run_variant_expression` in v1.0 "because the new name better describes its meaning" — https://testmon.org/blog/new-in-testmon-100/) lets a project partition the DB into named environments (e.g. keyed by Python version, OS, or any pytest-config-time expression) so that switching environments doesn't look like universal drift within one environment's data — it creates a **separate side of the DB per environment** instead of forcing a full run. This is testmon's answer to the "one DB, many environments" problem, in contrast to voci's current plan of a coarse-grained wholesale bail-out.

## 5. Interpreter/OS/git absence

- **Issue #214** ("fails when git is not present"): v2 assumes git is available (used to fetch file checksums from git when available — see the v2.0 blog note: "Testmon now retrieves file checksums from git when available"). In a git-less container, this produced a full `INTERNALERROR` crash rather than a graceful fallback to filesystem hashing. This is a hard regression class: a documented feature ("use git for speed") became an undocumented hard dependency.
- No explicit interpreter-version bail-out was found documented; testmon relies on the DB-format-version check plus the environment_expression mechanism (if a project chooses to key on interpreter version) rather than auto-detecting a Python version change as its own invalidation category.

## 6. Failing tests always re-run

**Blog:** https://testmon.org (homepage summary), version-12 blog (https://testmon.org/blog/version-12-is-out/)

- "**Testmon always re-executes tests which failed last time**," stated as a hard guarantee, unconditional on the dependency graph. This was actually a *fix*, not original design: earlier versions cached and **re-reported** a stale failure without re-running it, which the changelog calls out as broken UX — "This previous behavior was imperfect in many ways and sometimes spooked users."
- Related bug history: **#90** ("`--testmon -x` runs a test always, although there is a failing one already") and **#99** ("combination of `--testmon` and `--tlf` will execute failing tests multiple times in some circumstances") show this "always re-run failures" rule interacting badly with `-x`/last-failed-first ordering — edge cases where a failing test re-runs more than once per invocation, a correctness-adjacent bug in the union-with-last-failure logic, not a design gap.
- **Issue #92** ("Rerun tests should not fail") suggests users expect re-running a previously-failed-but-now-fixed test to reliably reflect the new state; treated as closed/non-issue eventually.

## 7. Deselection interaction with `-k` / `-m` and other pytest selectors

**Blog:** https://testmon.org/blog/new-in-testmon-100/ ; **Issue:** https://github.com/tarpas/pytest-testmon/issues/187

- v1.0 fixed general confusion when tests were deselected by means other than testmon itself: "Old versions of testmon got confused if you deselected some tests through other means than testmon itself."
- **`--testmon-noselect`** is documented as being **auto-forced** whenever `-m`, `-k`, `-l`, `-lf`, or an explicit `test_file.py::test_name` selector is used — testmon deliberately steps back to "reorder only, don't deselect" rather than try to intersect its own selection logic with pytest's, because getting that intersection wrong is worse than doing nothing. This directly matches voci's plan's "bail-out set... trades directly against how often the feature helps" framing, but testmon resolves it as a **hard rule keyed on flag presence**, not a judgment call per invocation.
- **Issue #187** ("`--ignore` not ignoring file in conjunction with testmon"): a real bug in that intersection — `--ignore` was silently not respected when testmon selection was also active. Shows the class of bug this "force noselect" rule is meant to prevent, still leaking through in one specific combination.

## 8. `--testmon-forceselect` / `--testmon-noselect` / `--testmon-nocollect`

**Sources:** search-aggregated from README/blog/GitHub source (`pytest_testmon.py`); confirmed defaults via issue #259, #227.

- `--testmon` — full default behavior: select + collect (record) in one run.
- `--testmon-forceselect` — select only tests affected by changes **and** satisfying pytest selectors simultaneously (used to combine testmon's own filter with `-k`/`-m` deliberately, opposite of the auto-forced noselect above).
- `--testmon-noselect` — reorder tests (affected-first) but run everything; auto-forced under `-k`/`-m`/`-l`/`-lf`/node-id selection. As of 1.3.x it also sorts *within* each group by speed (fastest first).
- `--testmon-nocollect` — run testmon but **don't write** new dependency data; auto-forced when running under a debugger or under coverage (since testmon uses coverage.py internally and the two can't share the coverage-collection hook — see §9).
- **Issue #227** (open): `--testmon-noselect` + `--testmon-nocollect` together currently collapse to `--no-testmon` entirely rather than composing (a known, unfixed combinatorial bug in the flag-resolution logic).
- **Issue #259**: `--testmon-nocollect` combined with `pytest-xdist` causes intermittent `sqlite3.OperationalError: database is locked`, root-caused in the report itself: the `TestmonXdistSync` plugin (which propagates a shared `testmon_exec_id` from controller to workers) is registered **only inside the `should_collect` branch**, so under `--testmon-nocollect` every xdist worker independently calls `fetch_or_create_environment()` with a write-locking `BEGIN IMMEDIATE TRANSACTION`, and they race. This is a structural bug from bolting xdist support onto a codebase whose locking model assumed a single writer.

## 9. Coverage.py interaction

**Issue:** https://github.com/tarpas/pytest-testmon/issues/181 ; **Issue:** https://github.com/tarpas/pytest-testmon/issues/223

- testmon uses coverage.py **internally**, and this makes it **incompatible with running `pytest-cov` concurrently for a separate purpose** in the same invocation: "collection automatically deactivated because it's not compatible with coverage.py" — testmon silently turns off its own data-writing (falls back to select-only) rather than erroring, when it detects `pytest-cov` is also active.
- Maintainer confirms this is by design ("everything looks as designed to me") but concedes **it should be documented and isn't**: "coverage.py is used internally by testmon, and it's not possible to collect coverage and testmon data simultaneously." User reply: "You should write this in docs." (unresolved — the issue closes without a docs fix ever confirmed).
- **Issue #223**: testmon **hard-errors** (raises `TestmonException`) if the user's coverage config has **branch coverage** turned on: `"testmon doesn't support simultaneous run with pytest-cov when branch coverage is on. Please disable branch coverage."` No plan to support it ("Not at the moment").

## 10. Stale DB / DB format / DB size / corruption

- **DB is SQLite** (`.testmondata`, plus WAL/SHM sidecar files from `PRAGMA journal_mode = WAL`).
- Schema version is embedded; a version mismatch → **"new DB"**, i.e. silently discard and rebuild rather than migrate or warn loudly. (v1.2: "Database schema was updated to version 8, thus previous versions of pytest-testmon are not compatible with 1.2.0.")
- **Issue #188** ("Remove testmondata automatically if old") — closed, implying some staleness-based auto-removal was added, but no detail found on the exact staleness criterion.
- **Corruption / locking bugs**, recurring across years — evidence this is a chronically fragile subsystem, not a one-off:
  - #203, #68, #75 — `sqlite3.OperationalError: database is locked` at various points (some closed/fixed, recurring under new circumstances each time, e.g. xdist in #259).
  - #205 — "Testmon db still corrupted with `--collect-only`."
  - #234 — writing to DB failed with "attempt to write a readonly database," a distinct case from the xdist one.
  - #169 — "Windows: Collection fails and breaks DB."
  - #114 — "using of debugger corrupts the testmon database" (this is presumably *why* `--testmon-nocollect` is auto-forced under a debugger, per the flag doc above — reactive fix, not designed in from the start).
  - #164 — "missing dependencies after import error": "After a test run with import error the db could be in incorrect state (a test case is not taken into account)" — an under-approximation caused by an unhandled error path, i.e., a partial/failed write leaves the DB *silently* missing dependency edges rather than refusing to trust that data.
  - #233 — **"`.testmondata` does not get flushed at exit"**: relying on `__del__` to close the sqlite connection is unreliable in CPython (not guaranteed to run), so under certain import patterns (reproduced with a trivial `dataclass` + `Iterator[Foo]` type-alias pattern from pytest-mock) the DB connection is never explicitly closed, and only the WAL file — not the base `.testmondata` — carries the latest data. This bites specifically when **archiving/copying `.testmondata` in CI** without also copying the `-wal`/`-shm` sidecars (see §11) — a maintenance gap (should call `close()` explicitly, doesn't) rather than a fundamental limit.
  - #219 — WAL/SHM sidecar files appearing/persisting unexpectedly inside containers, interacting with xdist ("different tests were collected between gw1 and gw0") — never fully root-caused, closed without resolution ("Unfortunately, I failed to make any progress").

**No documented DB size figures were found** — no numbers given anywhere in the blog/docs for how big `.testmondata` gets on a large suite; this appears to be an unaddressed topic on their side (voci's plan flags "cache growth" itself as risk #8 with no upstream comparison available).

## 11. CI usage, database transfer across machines/branches

**Blog:** https://testmon.org/blog/testmon-in-ci/, https://testmon.org/blog/better-github-actions-caching/ ; **Issues:** #236, #251, #77, #78

- Stated position, directly: **"pytest-testmon doesn't have any CI-specific behavior — it runs the same locally and in CI."** They do not ship CI-specific tooling; local reproduction is the recommended debugging path.
- Their actual recommended solution for CI is **not** "cache `.testmondata` between runs" — it's **testmon.net**, a paid hosted service ("maintains a centralized database of dependencies and test results" via a `--tmnet` flag that swaps SQLite persistence for the cloud API). Explicit statement: **"There is no synchronization between local `.testmondata` and testmon.net"** — the two persistence backends are non-interoperable, you pick one.
- **Issue #236, #251** (open, unresolved, recurring across many separate reporters): caching `.testmondata` via `actions/cache` in GitHub Actions and restoring it in a PR run reliably fails to select anything — testmon reports **"new DB, environment: default"** even though the file was restored, or reports the packages-changed message. Root causes surfaced in the thread:
  - The **WAL file** (`-wal`) holds uncommitted/recent writes; if only `.testmondata` is cached/copied and not `-wal`/`-shm`, the restored DB is empty or stale. Workaround given: force a WAL checkpoint before archiving (`PRAGMA wal_checkpoint(TRUNCATE)`) or disable WAL mode (`PRAGMA journal_mode = OFF`) so everything lands in the single file.
  - Even with WAL handled, the packages-changed false-positive (§4) independently causes full reruns.
  - Maintainer's explicit, blunt statement on this whole class: **"In general transferring `.testmondata` across computers and even operating systems is not tested and therefore not supported. https://testmon.net is intended for that."** This is a major admission: the free/local product's core CI use case (share the DB across CI runners, or between CI and a developer's laptop) is **explicitly unsupported**, with the supported path being a paid product.
- **Branch switching**: **Issue #78** ("Remember history for changed files?!") — switching git branch A → B → A causes the DB to be overwritten by branch B's data on step 2, so step 3 (back on A) sees no benefit and reruns/recollects fully, because **testmon retains only the most recent run's state, not per-branch history**. `environment_expression` could be keyed on branch name as a workaround, but the maintainer notes this only helps for **long-lived branches** — an unknown/new environment value just forces a full run anyway, so it doesn't solve short-lived feature branches. Maintainer's own assessment of doing this properly: "not sure how practical this would be... the real challenge would be garbage collection. Until what time would the branch-specific information be retained?" — i.e., they consider proper per-branch history a large, still-unsolved design problem, not merely unimplemented.
- **Issue #77** ("merge testmon databases", open, years old): no support for combining `.testmondata` files produced by separate parallel CI shards/builds. Manual SQL-level merging is possible in principle (unique key on `(node_variant, node_name)` in the node_file table) but no tool ships for it. This is the direct analog of "distributed/sharded CI run, want one merged map" — unsolved upstream.

## 12. xdist / parallel execution

**Blog:** https://testmon.org/blog/v14-with-xdist-support-is-out/ ; **Issues:** #42, #245, #259, #207, #170

- For years (pre-1.4.0) **testmon and xdist were flatly incompatible** — announced as a headline fix: "For many years it hasn't been possible to use pytest-testmon and run tests in parallel via pytest-xdist," calling parallelization "probably the most general way to speed up a large test suite" (i.e., they saw xdist and testmon as two independent, both-important speedup axes, not substitutes).
- Mechanism for making it safe (inferred from issue #259's bug report, which described the intended architecture): a controller process holds `testmon_exec_id`/writes; xdist workers call `TestmonData.for_worker()` (read-mostly) rather than `for_local_run()`, and a `TestmonXdistSync` plugin propagates state from controller to workers via `pytest_configure_node`. This is precisely the single-writer/many-reader design a correct implementation needs — but see #259, where a registration-order bug breaks that discipline specifically under `--testmon-nocollect`.
- **Issue #207** ("testmon does not respect test order," using `--dist loadfile`): user has a **deliberately order-dependent test** (one test writes a file, the next reads it) and expects `--dist loadfile` to preserve intra-file order under xdist+testmon; behavior was buggy/undefined in their combination. This is a case where testmon's *selection* correctly has no way to represent an inter-test file-based dependency at all (matches voci's plan's "order dependence" bullet) compounded by an xdist scheduling interaction.
- **Issue #170**: `--numprocesses` (xdist's long flag for `-n`) wasn't rejected/handled the same way as `-n` in testmon's argument validation — a smaller compatibility bug, now fixed.
- **Issue #245** (fixed in v2.2.0): xdist + a new dependency appearing mid-run → crash because the DB was opened read-only for workers but the package-change code path unconditionally tries to write (see §4/§10).

## 13. Subprocesses / multiprocessing

**Issues:** https://github.com/tarpas/pytest-testmon/issues/16, https://github.com/tarpas/pytest-testmon/issues/192

- **Subprocess measurement existed once and was removed**: v1.0.0 blog explicitly states "subprocess-based measurement" capability was **removed** in that release (issue #16 shipped it originally in the 0.x line, but v1.0 dropped it) — an admission that this was tried and pulled back, not merely never attempted.
- **Issue #192** ("multiprocessing does not seem to be supported," still open as of 2.0.9): reproduced case where a test spawns a `multiprocessing.Process` that calls into project code; a change in that code is **never detected** because coverage.py's tracer (which is what testmon relies on) doesn't follow into the child process by default. Maintainer's response is essentially "confirmed, no fix, vote if you care": **"Correct. Users are welcome to react or comment to this issue so that I know how important it is."** A follow-up suggests integrating with `pytest-cov`'s subprocess-coverage support (`coverage.process_startup` + `COVERAGE_PROCESS_START` env var) as the likely fix path, but nothing shipped.
- Directly parallels voci's plan's bullet "**Subprocesses**, including `@voci.isolated`, whose imports happen somewhere the parent's graph is not looking" — testmon's experience shows this is a real, reported, still-open pain point for real users, not a theoretical concern.

## 14. Installed / non-editable packages, `site-packages`, `src/`-style layouts

**Issue:** https://github.com/tarpas/pytest-testmon/issues/208 ; **Issue:** https://github.com/tarpas/pytest-testmon/issues/206

- **Issue #208**: user wants to run testmon against an **installed (non-editable) package** by pointing pytest's rootdir at `site-packages`. Result: "testmon doesn't detect changes in the package code, only the tests themselves." Maintainer's explanation of the underlying rule: **"testmon includes pytest rootdir in its coverage and excludes `sys.prefix`."** Their stated reasoning for not doing better: "Including everything is prohibitive performance-wise and I don't have a clear idea how to support multiple directories," and separately, portability of the DB across machines with different absolute paths is an open problem blocking a fix ("I don't have a clear idea how to support python paths which are portable across systems"). Their explicit, final answer: **"For the coming months... testmon only supports editable installs where everything you need to track is under pytest rootdir."** This is *exactly* the failure mode voci's plan calls "The source-root gap" / non-editable-install sharp case — testmon hit the identical wall and settled for a documented restriction rather than a detection-and-warn mechanism.
- **Issue #206**: a **more insidious variant** of the same root-scoping rule — if the project's virtualenv happens to be created *inside* the rootdir tree (e.g. `venv/` under the project directory, with tests also under that tree), testmon fails to detect changes to project code at all, in a way that looked like a total-failure bug until the maintainer traced it to the venv-inside-rootdir path overlap. Fixed by advising users to relocate the venv outside rootdir — never fixed at the root-scoping-logic level, workaround-only.

## 15. `-k`/filename heuristics and collection edge cases

- **Issue #91** ("testmon sometimes runs too many tests if the test filename ends in `_test`" instead of `test_`): a naming-convention edge case in how testmon maps file-level identity to node ids caused intermittent over-selection (arguably safe-direction but surprising/non-deterministic to the user — "seems to run an arbitrary number of tests each time if I change/revert the same file"). Traced to ambiguity between `a_test.py` and `test_a.py` naming schemes.
- **Issue #104** ("`test_file.py::test` dependency incorrectly computed"): "In the common case when the first line of a file is not in AST (because it's a comment or directive) testmon misses the dependency of tests on the highest module level (function definitions, decorators, etc)." Concrete practical consequence stated: **"it's not possible to unskip a skipped test"** — i.e. removing a `@pytest.mark.skip` decorator, which is itself the first-line-adjacent AST node, sometimes fails to register as a change. A parsing/off-by-one-in-AST-block-boundary bug, not a fundamental limit, but shows how brittle the block-boundary-to-checksum mapping is against decorator-level edits — directly relevant to voci's "fingerprint by source text per qualname" design choice, since testmon's line/block-boundary approach visibly breaks in exactly the seam voci's plan avoids by hashing by function rather than by line range.
- **Issue #240** (`setup_class` not captured): editing a `unittest`-style `setup_class`/xunit-style setup method only triggers a re-run of the *first* test in the class, not siblings that depend on the shared state it establishes — this is testmon's own version of voci's "shared-scope fixture" hole (Option B's plan explicitly addresses this via "static fixture union... closes the shared-scope hole"). testmon has **no equivalent static-union fix**; it purely traces, and tracing under-attributes exactly as voci's plan predicts for a scope="session"-style fixture.
- **Issue #204**: collecting a subdirectory then later collecting the whole tree leaves the DB believing only the subdirectory's tests exist (`collected 0 items / 570 deselected / -570 selected`), i.e. **partial-scope runs poison the "known test universe"** rather than being treated as a subset view. Never fully reproduced/fixed in the thread (closed due to inactivity) but real and reported by multiple users with a docker-based repro.

## 16. Validation / trust-building tooling (proposed, not built)

**Issue:** https://github.com/tarpas/pytest-testmon/issues/89

- A user asked for a **dry-run / shadow mode**: run all tests regardless, but report which ones testmon *would have* skipped, and loudly warn if any such test's outcome differs from last time (i.e., testmon's prediction would have been wrong). Explicit motivation: **"This would help convince my team that we can trust testmon's implementation... without the risk of missing any test failures due to a testmon bug."**
- Maintainer's counter-proposal was narrower but philosophically telling: track **inconsistencies between runs where the outcome changed but testmon predicted it wouldn't**, and attribute the likely cause: "In real world most of the inconsistencies would be dependencies between tests and changes which testmon doesn't detect yet (build environment/scripts, requirements.txt change, etc.)." This is effectively an internal acknowledgment that most false-greens are known-and-named categories (env/build-script drift), not novel/unknowable ones.
- **Never shipped.** Still open, years old. This is worth flagging explicitly to your plan: testmon has never built the "trust but verify" tooling that would let users detect under-approximation empirically before it costs them a real bug — the tool asks for blind trust in the bail-out set's completeness.

---

## Missing from our plan's "Failure modes" / "Risks" sections

These are documented testmon problems/gaps that don't appear (or appear only partially) in `/home/hubert/voci/plans/testmon-plan.md`'s "Failure modes" and "Risks" sections:

1. **Cross-machine / cross-CI DB transfer is unsupported, full stop.** testmon's own maintainer: "transferring `.testmondata` across computers and even operating systems is not tested and therefore not supported." The plan's Risks list "environment drift" as a bail-out trigger, but doesn't address the more basic problem of *whether the map/DB can even be moved between the machine that built it and the machine that wants to consume it* — WAL/SHM sidecar files, filesystem path portability, and lock-file semantics all bite here. If voci intends the map to be shared across CI runs or between local and CI, this needs its own line item.

2. **Branch switching loses history — not just goes stale.** The plan doesn't mention that a bidirectional branch switch (A → B → A) causes total loss of A's map when B's run overwrites it, forcing a full rebuild on return to A even though nothing on A actually changed. This is different from "mtime churn" (which the plan does cover) — it's about the map only ever holding the *last* run's state, with no per-branch or per-commit retention, and the maintainer explicitly called correct branch-aware retention an open, possibly-intractable garbage-collection problem.

3. **SQLite connection lifecycle / explicit flush.** Relying on Python object destruction (`__del__`) to close/flush the DB is unreliable and testmon hit this concretely (#233) — data silently sits in a WAL file that isn't visible if only the base DB file is archived. This is an implementation-discipline risk specific to choosing SQLite for storage that the plan's "Storage... sqlite is the likely answer" line doesn't flag.

4. **Multiprocessing/subprocess is not just "the parent's graph doesn't see it" — it's been tried and abandoned.** The plan lists subprocesses as an under-approximation bullet, correctly, but doesn't note that testmon *shipped* subprocess-based measurement once and pulled it back in v1.0, nor that a live open issue (#192) with a maintainer "vote if you care" response is the multi-year status quo. Worth knowing this isn't a minor edge case to defer — it's a feature testmon considered and gave up on.

5. **Concurrent use of two coverage-consuming tools conflicts outright**, and worse, **branch coverage mode is a hard incompatibility** (`TestmonException` raised, coverage/testmon mutually exclusive when branch coverage is on). Voci's `PY_START` design sidesteps needing coverage.py, but if voci's own coverage feature (`voci/_run/coverage.py`) and the future testmon-like tracer both want to instrument the same run, there may be an analogous "can't run both measurement mechanisms in the same process" constraint worth checking early rather than discovering at integration time. Not in the plan's Risks list at all.

6. **A dry-run/self-verification mode was requested by users and never built.** The plan doesn't propose (or reject) an analogous "run everything, but flag every case where the map would have wrongly skipped" verification mode. Given the plan's own framing that under-approximation is "silent and indistinguishable from a real pass," testmon's failure to ever ship the trust-building tool its own users asked for is a cautionary data point: if voci wants adoption despite the risk class, an opt-in verification mode may be the one thing that actually earns trust, and it's cheap to design in from the start rather than bolt on later.

7. **Filename/collection-scope edge cases** (#91's `_test.py` suffix ambiguity, #204's partial-directory-collection poisoning the DB's notion of "all tests") aren't analogous to anything in voci's plan — these are testmon-specific implementation quirks from how it maps file-scoped collection to its persistent state, but they're a warning that **the "known universe of tests" bookkeeping itself is a source of false negatives**, independent of the dependency-graph/tracing correctness the plan focuses on. Worth a line in Risks about what happens if voci is invoked against a subset of `testpaths` and then later against the full set — does the bail-out set include "test universe changed since last run"?

8. **DB size was never quantified by testmon's own docs** — the plan's risk #8 ("cache growth") has no upstream data point to compare against; this is a gap in *available information*, not a gap in testmon's design, but worth noting since the research task asked specifically about database size and testmon simply never publishes numbers on it.
