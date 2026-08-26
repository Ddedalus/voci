# Phase 4 Exit Suite Selection: pytest→velox OSS Corpus

Companion to [migration-tool-plan.md](migration-tool-plan.md) § 4, defining the real-world test suites for Phase 4's end-to-end migration verification, prefactor codemods, and postfactor skills.

## Context

Phase 4 closes with "one real OSS suite migrated end-to-end through the full ladder, written up." The suite must be:
- Large enough to exercise the full pipeline and uncover edge cases
- Clean enough to convert without excessive refusals
- Representative of real pytest patterns (conftest graphs, parametrization, hazards)
- Popular enough that the write-up carries weight

The extraction and audit stages are deterministic; the conversion and prefactor/postfactor phases are where real decisions live. A good exit suite has **rungs in the ladder** — places where `extract → audit` finds refusing constructs, and prefactor or postfactor skills have honest work to do.

## Evaluation Methodology

Scanned ten popular OSS projects across the velox-migrate matrix's refusal/serialization rows:

| Construct | Code | Disposition | Impact |
|-----------|------|-------------|--------|
| `request.getfixturevalue(computed)` | VX012 | refused | breaks signature rewrite |
| `request.addfinalizer(...)` | VX014 | refused | finalizer placement |
| `request.getfixturevalue` of ambiguous name | VX028 | refused | resolution ambiguity |
| `indirect` parametrization incompatible with `params=` | VX029 | refused | case list collision |
| `pytest.raises(E, func, *args)` | VX210 | unsupported | must rewrite to `with` form |
| `capfd`, `capsysbinary`, `capfdbinary` | VX203 | unsupported | capsys is the velox equivalent |
| `caplog.text`, `.record_tuples`, `.clear()`, `.handler` | VX206 | unsupported | API mismatch |
| `tmpdir`, `tmpdir_factory` | VX208 | unsupported | `tmp_path` is the only builtin |
| `pytest.warns`, `recwarn`, `deprecated_call` | VX216 | unsupported, serializing | warns requires solo execution |
| `@pytest.mark.filterwarnings` | VX108 | unsupported, serializing | incompatible with velox's per-test patching |
| conftest hooks (`pytest_configure`, `pytest_collection_modifyitems`, …) | VX022 | unsupported | cannot hook into velox collection |
| monkeypatch | VX401 | hazard, serializing | process-global state mutates under concurrency |
| `os.environ` writes | VX402 | hazard, serializing | idem |
| `sys.modules` surgery | VX405 | hazard, serializing | idem |

---

## The Candidates

### marshmallow — The Clean Proof

| Metric | Value |
|--------|-------|
| Test count | 1188 measured (652 was a count of test functions, not of collected cases) |
| Fixture graph | 19 fixtures / 1 conftest |
| Plugin dependencies | **none** |
| Estimated refusals | **none** |
| Monkeypatch uses | 0 |
| Hazardous constructs | 0 |

**Strengths:**
- Zero disqualifying constructs; entire audit should be "mechanical" or "marker".
- Simple conftest with a real fixture graph: `blog(user)`, `serialized_user(user)`.
- No pytest hooks, no unittest.TestCase, no xunit setup, no installed plugin fixtures.
- Only ini setting is `norecursedirs` (VX303, mechanical).

**Why not pick it alone:**
- Single conftest means Phase 3's override specialization machinery never fires — too clean to be representative.
- No hazards means the concurrency estimate is simply "run everything in parallel" — valid but not interesting to write up.
- Missing the ladder: no refusals to prefactor, no postfactor skills to apply.

**Role:** Smoke test for the extractor and audit stages. If marshmallow's audit comes back non-mechanical, the tool has a bug.

**Outcome (migrated):** the audit did come back mechanical and the conversion was still broken — 16 class-scoped fixtures the matrix had no row for. 1183 of 1188 tests pass under velox; see [marshmallow-migration.md](marshmallow-migration.md).

---

### flask — The Ladder

| Metric | Value |
|--------|-------|
| Test count | 386 (31 parametrized, 7 classes) |
| Fixture graph | 17 fixtures / 1 root conftest + 2 example conftests |
| Plugin dependencies | coverage only |
| Estimated refusals | VX210 ×19, VX401 ×77, VX405 ×6, VX017 ×1 |
| Monkeypatch uses | 77 |
| Serializing hazards | VX401 (monkeypatch) spans entire suite |

**Audit findings:**
- `_standard_os_environ(monkeypatch)` is autouse in root conftest → entire suite serializes under naive rules (VX401).
- 19 sites of `pytest.raises(E, func, ...)` (VX210) — textbook prefactor target: rewrite to `with` form while suite is green.
- `test_apps` fixture and `purge_module` helper do `sys.modules` surgery (VX405) and hold `request` in closures for `addfinalizer` (VX014/VX017) — honest refusals showing the limits.
- Multiple conftests (root + examples) without overrides, so layout is straightforward.

**Why pick this:**
- **The concurrency arc:** Naive conversion shows 100% serialization (monkeypatch). After prefactor unwinding the autouse fixture, actual concurrency is regainable — a concrete, measurable win to document.
- **Real work:** Prefactor codemod runs `pytest→pytest` rules while suite is green; postfactor skills help clean up after conversion; residual refusals are written up honestly.
- **Weight:** 68k GitHub stars; "we migrated Flask's test suite" has authority.
- **Representative:** Fixtures, parametrization, hazards, conftest layout — the full ladder.

---

### rich — The Volume Check

| Metric | Value |
|--------|-------|
| Test count | 721 (35 parametrized, 0 classes) |
| Fixture graph | 5 fixtures / 1 conftest |
| Plugin dependencies | pytest-cov only |
| Estimated refusals | none found |
| Monkeypatch uses | 19 |
| Serializing hazards | VX401 (autouse monkeypatch) |

**Audit findings:**
- Single autouse fixture: `reset_color_envvars(monkeypatch)` — same 100% serialize trap as flask, but trivially prefactorable (just remove the `autouse` mark and call manually).
- Almost no fixture graph; tests are mostly independent.
- Near-zero wiring constructs.

**Why useful:**
- Largest suite in the list; good for measuring throughput and performance impact.
- Before/after concurrency numbers are stark: 100% → ~99% (only 19 tests need the cleanup).
- Minimal conftest complexity, so the focus is on scale, not design.

**Role:** Secondary proof of the prefactor pathway on a larger suite. If flask is the "ladder," rich is the "before/after scale chart."

---

### jinja — The Middle Ground

| Metric | Value |
|--------|-------|
| Test count | 691 (32 parametrized, 45 classes) |
| Fixture graph | 21 fixtures / 1 conftest |
| Plugin dependencies | pytest-timeout, trio |
| Estimated refusals | VX307 (filterwarnings=error) blocks conversion, VX014 ×1 |
| Monkeypatch uses | 6 |
| Async story | parametrized fixture returning asyncio.run or trio.run |

**Audit findings:**
- Fixture chain exercises params= rewrite path: one fixture returns `request.param` over parametrized `_asyncio_run` and `trio.run` (Phase 3 indirect handling).
- `filterwarnings = ["error"]` in pytest.ini (VX307, unsupported) — warnings cannot be treated as failures under velox's per-test isolation.
- One `request.addfinalizer` (VX014) in a test that cannot convert; refusal is honest and pointed.
- Trio dependency; the async story is present but not the core migration path.

**Role:** Exercises the parametrization ladder (especially `params=` conversion) and the async ecosystem. Secondary candidate if flask proves insufficient.

---

### httpx2 — The Async Story

The row below is **measured**, against `pydantic/httpx2` rather than upstream httpx: it is the
audit reported in [httpx2-audit.md](httpx2-audit.md), which supersedes the extrapolated httpx
numbers that stood here (539 tests, "VX206 ×2", 7 fixtures) and were wrong about the scale by
almost four times.

| Metric | Value |
|--------|-------|
| Test count | 1991 collected, 1973 green under pytest in 27.3s |
| Fixture graph | 14 fixtures / 2 conftests, **0 overrides** |
| Plugin dependencies | anyio, pytest-trio, pytest-httpbin, pytest-codspeed, flaky |
| Blocked | 342 tests (17.2%), 294 of them the trio half of `anyio_backend` |
| Serializing hazards | VX402 ×5 — one autouse `clean_environ` reaches 1739 tests |
| Naive serial share | 88.0% |

**Audit findings:**
- anyio parametrizes `anyio_backend` over `("asyncio", "trio")` whenever trio is installed, so
  every async test exists twice and half of it has no loop to run on (VX324 ×23, 294 cases). A
  suite-level `anyio_backend` returning `"asyncio"` is the prefactor, and it is one fixture rather
  than a codemod.
- `clean_environ`, autouse in the root conftest, is the whole 88% — flask's arc on four times the
  suite.
- `filterwarnings = ["error"]` suite-wide (VX307) has no velox spelling, so warnings stop failing
  after conversion. No stage of the pipeline catches what that hides.
- Small honest drops: 34 codspeed benchmarks, 6 httpbin, 7 `@pytest.mark.trio`, 12 `pytest.warns`,
  9 `filterwarnings` tests.

**Role:** the Phase 4 exit suite. Four times flask's size, the same concurrency arc, a real
plugin-wired async story, and no override chains — which is also its one weakness as a corpus:
like marshmallow, it never fires the specialization machinery.

---

## Candidates to Skip

### starlette
**Blocker:** `tmpdir` used 130 times (VX208, unsupported). A prefactor `tmpdir→tmp_path` codemod would need to exist and run before general conversion. Viable as a *fourth* pass (after the extraction, prefactor rules, and codemods layer), but out of scope for Phase 4's initial ladder. Revisit for Phase 5 or as a postfactor skill demo.

### structlog
**Blockers:** 56 `pytest.warns` uses (VX216, unsupported and serializing); `time-machine` plugin (VX408, hazard); `asyncio_mode = auto`; pytest-randomly. The warns usage alone makes this a bad fit — it would require both conversion (impossible) and prefactor unwinding (56 sites), and time-machine adds another serialization source. Too complex for Phase 4.

### attrs
**Blockers:** Hypothesis plugin (VX030/VX323, unsupported); `pytest_configure` hook (VX022, unsupported). Both require special handling beyond the core codegen.

### click
**Blockers:** 40 `capfd` uses (VX203, unsupported); `filterwarnings = "error"` (VX307, unsupported); pytest-randomly. Prefactor would need to rewrite to capsys, which is non-trivial. Similar issue to starlette.

### uvicorn
**Blockers:** pytest-mock (VX219, serializing); 12 `pytest.param(marks=)` sites (VX102); tests that bind real network ports (VX413, hazard). The network binding is the killer — under concurrent execution the tests interfere with each other, making any run result unreliable.

---

## Recommended Execution Order

### Phase 4 Staging

1. **marshmallow (smoke test)**
   - Extract, audit. Expect all mechanical/marker.
   - If audit shows unexpected refusals, stop and fix the tool.
   - If clean, proceed to verify stage (once implemented).

   Done — see [marshmallow-migration.md](marshmallow-migration.md). The audit was clean and the
   conversion was not.

2. **httpx2 (exit suite + write-up)** — flask held this slot until httpx2 was scoped; the numbers
   above are why it changed hands. Audit done, see [httpx2-audit.md](httpx2-audit.md).
   - Prefactor (pin `anyio_backend` to asyncio), convert, verify, concurrency triage.
   - Document the arc: naive conversion = 88% serialized on one autouse `clean_environ` →
     unwinding it recovers concurrency.
   - Write up what stays under pytest and why, and the VX307 divergence nothing catches.
   - Measure throughput before/after; report in the write-up.

3. **flask or rich (optional secondary)**
   - If httpx2 alone suffices for Phase 4, skip.
   - flask is the one candidate with override chains and 19 `pytest.raises` call-form sites, so it
     is what to reach for if the prefactor tier needs a codemod to justify existing.

### Future Extensions (Phase 5+)

- **starlette:** After a tmpdir→tmp_path prefactor codemod is built.
- **jinja:** If indirect parametrization and params= rewrites need deeper exercise.

---

## Setup

Cloned suites are available at:
```
/tmp/claude-1000/-home-hubert-velox/b95a1df2-cd69-4010-8bcc-ade717835878/scratchpad/oss/
```

for local inspection. Re-clone into your own directory for persistence:
```bash
for r in flask rich marshmallow; do
  git clone https://github.com/pallets/$r.git
done
```

(Note: flask and jinja are under `pallets/`, marshmallow under `marshmallow-code/`, rich under `Textualize/`.)

---

## Verification Checklist

- [ ] marshmallow audit is all mechanical/marker
- [ ] flask audit identifies all expected VX210 sites
- [ ] flask autouse monkeypatch is flagged as VX401 serializing
- [ ] flask VX14/17 refusals (purge_module) are present and sited correctly
- [ ] Prefactor runs pytest→pytest rules on flask while test suite is green
- [ ] Conversion produces runnable velox code
- [ ] `velox --serial` passes (baseline single-threaded run)
- [ ] `velox` with default concurrency passes (post-prefactor)
- [ ] Write-up includes: audit findings, prefactor rewrites applied, before/after concurrency, refusals and their causes
