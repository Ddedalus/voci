# 12 — Migration: pytest → voci codegen

*"The migration codegen is a separate deliverable and the actual adoption bottleneck" (R§8.9). It is
also unusually tractable: name-based fixture graphs are **statically resolvable** from source plus
conftest scoping rules — which is exactly the analysis pytest performs at run time, done once, at
migration time.*

---

## 1. Position

voci ships **no runtime compatibility shims**. There is no `pytest` import alias, no
`conftest.py` support, no `@pytest.fixture` adapter. That decision is what keeps the runner small
and the hot path free of the node tree and hook protocol — but it means the whole compatibility
budget is spent here, in a one-time source transformation.

The tool is `voci migrate`, distributed with voci but importable and testable on its own. It is
**not** part of the v0.1 runtime and does not gate M1.

## 2. What it must handle

Ranked by how much of a real FastAPI suite each covers.

| # | Construct | Transformation |
|---|---|---|
| 1 | `@pytest.fixture` in `conftest.py` | → `@voci.fixture` in `tests/fixtures.py` (or a per-directory fixture module preserving the original layout). Scope carries over; `autouse` becomes an explicit `Depends()` on every test that was in its visibility scope. |
| 2 | Fixture parameters resolved by name | → `param=Depends(fixture_object)` + the import. **This is the core of the tool**: it must replicate pytest's resolution — nearest conftest wins, directory hierarchy, plugin fixtures, `request.getfixturevalue` — to know *which* fixture a name referred to. |
| 3 | `@pytest.mark.parametrize` | → `@voci.parametrize`, ids preserved verbatim so CI selectors keep working. |
| 4 | `@pytest.mark.skip/skipif/xfail` | → the voci equivalents, 1:1. |
| 5 | `@pytest.mark.<custom>` | → `@voci.tag("<custom>")`. |
| 6 | `@pytest.mark.asyncio` / `anyio` | Deleted. |
| 7 | `mock.patch` | **Left as-is** — stock `unittest.mock` keeps working. voci detects the decorator form itself; the tool adds `@voci.solo` to context-manager sites it cannot detect, and reports DI-seam opportunities ([08](08-patching-and-isolation.md)). |
| 8 | `request` object usage | `request.param` → the parametrized value; `request.getfixturevalue` → an explicit `Depends()` where statically resolvable, else flagged for review; the rest of `request` → `voci.test_info` or flagged. |
| 9 | `tmp_path`, `caplog`, `capsys` builtins | → voci's built-in fixtures. |
| 9b | `monkeypatch` builtin | No voci equivalent. `setattr`/`setitem` → `mock.patch` (auto-solo); `setenv` → an injected settings fixture where possible, else `mock.patch.dict`; `chdir` → `@voci.isolated`. Each rewrite is flagged for review. |
| 10 | `pytest.raises`, `pytest.approx`, `pytest.fail/skip/xfail` | → `voci.*` equivalents, mechanical. |
| 11 | Sync tests calling async code via `asyncio.run` / `loop.run_until_complete` | Flag: these will break on the shared loop. Suggest `async def` + `await`. |
| 12 | Plugins (`pytest-mock`'s `mocker`, `pytest-freezegun`, factory-boy fixtures, …) | Per-plugin recipes where a mapping exists; a clear "unsupported, here is the manual path" report otherwise. |

## 3. Architecture

- **Analysis on the pytest side.** The most reliable way to know what fixture a name resolves to is
  to ask pytest: run `pytest --collect-only` with a small voci-provided plugin that dumps, per
  test, the resolved fixture closure (`item._fixtureinfo`) as JSON. That converts the hardest part
  of the problem — replicating conftest scoping and plugin visibility — into "read pytest's own
  answer". Static-only analysis is the fallback for suites that cannot be collected.
- **Transformation with LibCST**, not `ast` + unparse: comments, formatting, and blank lines must
  survive, because the output is code humans will read and review in a diff.
- **Output is a normal git diff.** No in-place magic without `--write`; `--dry-run` prints a summary
  by category; every non-mechanical rewrite carries a `# voci: review — <reason>` comment.
- **Idempotent.** Running it twice changes nothing the second time.

## 4. The report

The deliverable is as much the *report* as the code:

```
voci migrate — tests/ (412 files, 3,180 tests)

  mechanical            2,941 tests   ✓ rewritten
  fixture wiring          204 fixtures → tests/fixtures.py
  patch sites              88 total
      → DI seam suggested          31   (highest value; review these first)
      → left as-is, auto-detected  44   (decorator form; voci marks these solo)
      → @voci.solo added          11   (context-manager form, undetectable statically)
      → @voci.isolated             2   (chdir / signal / C-type targets)
                                        55 tests will run solo — 1.7% of the suite
  needs review             47 tests
      asyncio.run in test   19
      getfixturevalue       12
      pytest-plugin usage   16   (pytest-freezegun: 16)
  unsupported               0
```

Knowing *before* migrating that 2.1% of the suite will be solo, and that 16 tests depend on a plugin
with no voci path, is what makes the decision to adopt rational.

## 5. Semantic gaps the tool must flag, not paper over

These are behavior changes migration cannot fix silently, and each gets an explicit flag:

- **Test order dependence.** Tests that passed only because of pytest's sequential order will fail.
  Suggested workflow: migrate, run with `--concurrency=1` first (should be green), then raise
  concurrency and triage what breaks. That two-step is the single most useful piece of migration
  documentation.
- **Shared mutable state** between tests (module globals, class attributes, singleton caches).
- **Session/module fixture teardown timing** ([04](04-dependency-injection.md) §3).
- **Blocking calls** — flag imports of known-sync libraries in test paths and point at
  `voci doctor` ([11](11-runtime-safety.md)).
- **`warnings.catch_warnings()` in a test body**, which re-points process-global state every
  concurrent sibling is reading ([11](11-runtime-safety.md) §3). Per-test `filterwarnings` marks
  carry over unchanged.

## 6. MVP (of the tool, in M3)

Categories 1–6 and 9–10 — the mechanical ~90% — plus the pytest-side closure dump, the LibCST
rewriter, the report, and `# voci: review` markers for everything else. Patch classification
(category 7) reports but does not rewrite in the first version.

## 7. Roadmap

- Rewriting routable `mock.patch` sites to `voci.patch` once the routing tier exists — a pure
  performance migration, opt-in and never required.
- DI-seam *suggestions*: where a patched target is already an injected dependency, propose the
  refactor as a separate reviewable diff. High value, and the thing that makes a migrated suite
  actually idiomatic rather than merely working.
- Per-plugin recipes: `pytest-mock`, `pytest-freezegun`/`time-machine`, `factory-boy`,
  `pytest-httpx`, `pytest-postgresql`.
- A `--check` mode for CI on partially-migrated repos.
- Reverse direction? No. Explicitly out of scope.

## 8. Open questions

- **Q25** — Does `voci migrate` require a working pytest collection of the target suite (proposed:
  yes, with a degraded static-only mode)? Requiring it is a real constraint for suites that only
  collect inside a container, but the alternative is reimplementing conftest resolution, which is
  the exact complexity voci exists to delete.
- **Q26** — Should migration preserve the conftest *layout* (one fixture module per directory that
  had a conftest) or consolidate into a single `tests/fixtures.py`? Preserving produces a smaller,
  more reviewable diff; consolidating produces the idiomatic result. Proposed: preserve by default,
  `--consolidate` to opt in.
- **Q27** — `autouse` fixtures expand to an explicit `Depends()` on every affected test, which can be a
  very large diff (an autouse DB-setup fixture touches every test in the suite). The alternative is
  a voci `@voci.fixture(autouse=...)`-style escape hatch — which would reintroduce implicit
  wiring, the thing being deleted. Proposed: expand explicitly, and let the diff size be an honest
  signal of how much implicit magic the suite carried.
