# 07 — Assertion Introspection

*The one premise the research refuted outright: lifting pytest's assertion rewriting is **cheap**.
Verbatim extraction was empirically performed during research and measured as a **30-line diff**
(R§2). Decision: vendor it, with three fixes. Reimplementing forfeits the 5.8k lines of upstream
tests that cover this feature, which are the real asset.*

---

## 1. What is being vendored

| Piece | LOC | Source |
|---|---|---|
| `rewrite.py` (AST pass + meta-path finder + pyc cache) | 1193 | `_pytest/assertion/rewrite.py` |
| `saferepr` | 155 | `_pytest/_io/saferepr.py` |
| Shim (config surface the rewriter expects) | ~130 | fresh |
| Explanation engine (`==` diffs: sequences, dicts, sets, dataclasses, text, streaming truncation) | ~600 | `_pytest/assertion/util.py` + `_compare_*.py` |
| PEP 657 caret fallback | ~50 | fresh |
| **Total** | **~2.1–2.4k** | |

Vendored under `velox/_vendor/assertion/`, with the upstream pytest commit hash recorded in a
`VENDOR.md` alongside the diff we apply. Re-vendoring is a deliberate act, not a tracked upstream
(risk 5 in [00](00-overview.md)).

The applicable upstream tests are ported alongside, with the pytest-specific harness swapped for
velox's. This is the single highest-leverage thing in the vendoring decision.

## 2. Mechanism (for readers who won't read pytest's source)

A meta-path finder at `sys.meta_path[0]` intercepts imports of test files only, with a name-based
early bailout costing ~5–8 µs per non-test import. Matching modules get one AST pass: each `assert`
is rewritten so every subexpression is hoisted into a fresh temp — **evaluated exactly once**, so
side effects are correct — followed by `if not <cond>:` that builds a `%`-formatted explanation from
`saferepr`s of the temps and raises. All expensive work lives inside the failure branch. Line
numbers survive via `ast.copy_location` fixups. Rewritten `.pyc`s are cached under a versioned tag
with mtime+size invalidation.

## 3. Measured costs (R§2)

| | |
|---|---|
| Passing assert overhead | **+4 ns** (`x == y`) to **+37 ns** (chained/boolop) vs plain. Scales with subexpression count, never with value size. |
| Cold import penalty | **4.6×** (parse + rewrite + compile vs plain compile); the pure-Python AST pass dominates at ~164 µs/assert. |
| Warm pyc load | **154× faster than cold.** The cache is load-bearing, not an optimization. |
| Bytecode size | 1.7–2× |

The passing-assert number is why this is a non-decision: introspection is free at run time, and the
entire cost is a one-time compile amortized by the cache.

## 4. The three required fixes

### 4.1 ContextVars for the module globals

`util._reprcompare`, `util._assertion_pass`, and `util._config` are module globals that pytest
save/restores per test item. **They are the one genuine concurrency blocker in the whole
subsystem** — and the fix is ~10 lines: make them `ContextVar`s. asyncio propagates context into
tasks automatically, so per-test values just work. The rewritten code itself is already
concurrency-safe: every temp is a frame-local (R§2).

### 4.2 Cache and identity hygiene

- Change the injected helper-module name (`rewrite.py:719` hardcodes `"_pytest.assertion.rewrite"`
  into generated pycs) to velox's own.
- Put a **velox rewriter version** in the pyc tag, alongside the Python magic number.
- **Include every codegen-affecting option in the pyc cache key.** pytest's
  `enable_assertion_pass_hook` is a documented footgun precisely for not being in it.
- Key the temp-pyc filename on `(pid, thread)`, not pid alone.
- Replace the `_writing_pyc` boolean guard with a real lock.

### 4.3 PEP 657 as the universal floor

pytest rewrites *only* test files: an `assert` inside a helper module raises a bare `AssertionError`
with no explanation. A ~50-LOC fallback that renders the `co_positions()` caret span (3.11+) for any
un-rewritten assertion closes pytest's own biggest fidelity gap at approximately zero cost (R§2).

```
tests/helpers.py:14: AssertionError
    assert resp.status_code == expected
           ~~~~~~~~~~~~~~~~~^^~~~~~~~~~
```

This also makes `--assert=plain` a usable mode rather than a punishment.

## 5. Cold-start guarantee

velox will be benchmarked cold in CI containers. **Never silently pay 4.6× per run** (R§2):

1. Resolve the rewrite cache dir: `--rewrite-cache`, else `VELOX_REWRITE_CACHE`, else a
   platform cache dir, else `sys.pycache_prefix` into a velox-owned directory.
2. Probe writability once at startup (create + delete a marker).
3. If unwritable: **warn on stderr, naming the path**, and fall back to `--assert=plain` with the
   PEP 657 floor. Record the fallback in the report header so the benchmark story is not silently
   corrupted.

A CI job in velox's own repo asserts the cold/warm ratio stays within budget.

## 6. Small things easy to miss

- Filter `@`-prefixed rewriter temps out of any locals display in tracebacks
  ([10](10-reporting.md)).
- `await` inside an `assert` works under the vendored rewriter — verified during extraction (R§2),
  and non-negotiable for an async-first runner, so it gets an explicit test.
- The explanation engine is *decoupled* from the rewriter and can be improved independently; it is
  also the highest-value part for users, so it stays in the MVP despite being the larger LOC chunk.

## 7. Rejected alternatives (recorded so they are not re-litigated)

| Alternative | Why not |
|---|---|
| Naive re-evaluation of the failing expression | Unsound with side effects. pytest shipped exactly this as `--assert=reinterp` and **deleted it in 3.0**. |
| Safe partial re-eval via `executing`-style introspection | ~80% fidelity at zero import cost. Genuinely attractive as a `--no-rewrite` mode for cold or read-only environments — kept on the roadmap, not the default. |
| Explicit `expect()` only | Ships as an option, never the only path. Forcing an API change on every assertion in a migrated suite is a non-starter. |
| Reimplementing the rewriter | Forfeits 5.8k lines of upstream tests for no benefit. |

## 8. MVP

Vendored `rewrite.py` + `saferepr` + shim with the three fixes; the comparison-diff explanation
engine; the PEP 657 floor; the cache-writability probe and fallback; `velox.raises`, `velox.approx`;
ported upstream tests.

## 9. Roadmap

- `--assert=introspect`: the `executing`-style partial re-evaluation mode, for read-only filesystems
  and truly cold one-shot runs.
- Rewriting user helper modules on request (`rewrite_modules = ["tests.helpers", "myapp.testing"]`),
  which pytest offers as `register_assert_rewrite` — cheap once vendored, and the PEP 657 floor
  makes it optional rather than necessary.
- Custom comparison explainers as plain callables registered on a type (the DI-shaped replacement
  for `pytest_assertrepr_compare`).
- Truncation policy tuning and a `--full-diff` flag.

## 10. Open questions

- **Q16** — Does velox rewrite *fixture* modules (`tests/fixtures.py`) by default? They are not
  named `test_*`, but assertions in fixtures are common and the PEP 657 floor is a weaker
  experience. Proposed: rewrite any module imported from within the discovered test roots, which is
  a superset of pytest's rule and costs one path check.
- **Q17** — Should the vendored code be reformatted to velox's style (ruff) or kept byte-identical
  to upstream for diffability? Proposed: byte-identical, with `# ruff: noqa` at the top of vendored
  files, because re-vendoring is the operation we need to stay cheap.
