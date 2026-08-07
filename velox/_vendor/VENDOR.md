# Vendored: pytest assertion subsystem

**Generated — do not edit.** Regenerate with `uv run python scripts/vendor_assertion.py`.

- Upstream: <https://github.com/pytest-dev/pytest> at `9.2.0.dev0-165-g28e86a6c2`
- Vendored: 2026-08-07
- Destination: `velox/_vendor/assertion/`

Why vendored rather than reimplemented: see [spec/07](../../spec/07-assertions.md) §1.
The 5.8k lines of upstream tests covering this feature are the asset; keeping the code
byte-identical apart from the edits below keeps them applicable, and keeps
re-vendoring cheap.

## Files

| Vendored | Upstream | Lines |
|---|---|---|
| `rewrite.py` | `_pytest/assertion/rewrite.py` | 1244 |
| `util.py` | `_pytest/assertion/util.py` | 201 |
| `truncate.py` | `_pytest/assertion/truncate.py` | 166 |
| `compare_text.py` | `_pytest/assertion/compare_text.py` | 141 |
| `highlight.py` | `_pytest/assertion/highlight.py` | 18 |
| `_typing.py` | `_pytest/assertion/_typing.py` | 42 |
| `_guards.py` | `_pytest/assertion/_guards.py` | 68 |
| `_compare_any.py` | `_pytest/assertion/_compare_any.py` | 155 |
| `_compare_mapping.py` | `_pytest/assertion/_compare_mapping.py` | 79 |
| `_compare_sequence.py` | `_pytest/assertion/_compare_sequence.py` | 103 |
| `_compare_set.py` | `_pytest/assertion/_compare_set.py` | 108 |
| `saferepr.py` | `_pytest/_io/saferepr.py` | 162 |
| `_pprint.py` | `_pytest/_io/pprint.py` | 705 |

Not vendored: `_pytest/assertion/__init__.py` (pytest's plugin glue). velox's equivalent
is `velox/_rewrite.py`. Everything pytest-specific those files imported is replaced by
`velox/_vendor/assertion/_shim.py` (~130 LOC, hand-written).

## Applied edits

22 recorded edits touching 62 upstream lines, plus 68 mechanical `_pytest.*` import rewrites.

### `rewrite.py`

- **threading import** (x1, 2 upstream lines) — needed by the (pid, thread)-keyed temp pyc and the pyc write lock below
- **hashlib import** (x1, 2 upstream lines) — needed to digest the codegen options into the pyc cache key
- **TYPE_CHECKING: AssertionState from the shim** (x1, 2 upstream lines) — velox does not vendor pytest's assertion plugin glue
- **injected helper-module name** (x1, 1 upstream lines) — upstream bakes '_pytest.assertion.rewrite' into every generated pyc; a velox pyc that imports pytest's rewriter at exec time would be both wrong and a hidden dep
- **pyc tag carries a velox rewriter revision** (x1, 2 upstream lines) — so a change to velox's vendored codegen invalidates stale pycs, not just a CPython magic-number bump
- **codegen options in the pyc cache key** (x1, 1 upstream lines) — pytest's enable_assertion_pass_hook is a documented footgun precisely because it changes codegen but not the cache key (spec/07 §4.2)
- **use the per-config pyc tail** (x1, 1 upstream lines) — pairs with the edit above
- **temp pyc keyed on (pid, thread)** (x1, 1 upstream lines) — velox runs tests concurrently in one process; two threads importing different test modules would otherwise race on the same temp file name (spec/07 §4.2)
- **_writing_pyc: thread-local guard + real lock** (x1, 3 upstream lines) — the upstream bool is both a reentrancy guard and a (non-)mutex; under threads it leaks across threads and serialises nothing (spec/07 §4.2)
- **_writing_pyc read** (x1, 2 upstream lines) — pairs with the edit above
- **_writing_pyc write** (x1, 5 upstream lines) — pairs with the edit above
- **get_cache_dir honours velox's resolved cache root** (x1, 1 upstream lines) — velox resolves and probes one cache root at startup (spec/07 §5) rather than scattering pycs into every source tree
- **_warn_already_imported without pytest's config-time warning plumbing** (x1, 8 upstream lines) — issue_config_time_warning is pytest plugin machinery velox does not have
- **always-true-assert warning import** (x1, 4 upstream lines) — same
- **always-true-assert warning class** (x1, 3 upstream lines) — same
- **velox support block (cache root, pyc tail, warning type)** (x1, 1 upstream lines) — fresh code, appended near the top so the rest of the file can reference it

### `util.py`

- **the three module globals become ContextVars** (x1, 12 upstream lines) — THE concurrency blocker: pytest save/restores these per test item, which cannot work when tests are concurrent asyncio tasks in one process (spec/07 §4.1)
- **crash repr without _pytest._code** (x1, 1 upstream lines) — pytest's ExceptionInfo is a large subsystem; the failure path here only needs a one-line 'where did the repr blow up' string
- **drop the bare `import _pytest._code`** (x1, 1 upstream lines) — pairs with the edit above; the generic import rewriter only handles `from` imports
- **velox naming in the repr-failure message** (x1, 1 upstream lines) — the string is user-visible; it should not say 'pytest_assertion plugin'

### `_compare_any.py`

- **velox's Approx, with an optional _repr_compare** (x1, 7 upstream lines) — velox ships its own approx (spec/07 §8); the MVP one is scalars-only and has no detailed diff to offer, so the summary line has to stand on its own
- **_velox_approx_compare helper** (x1, 1 upstream lines) — fresh, so the branch above stays readable
