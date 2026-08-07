# 03 — Discovery and Collection

*Pytest spends 0.87 s collecting 5000 tests. Only ~8% of that is importing the test modules; ~40% is
instantiating **two** node objects per test and ~26% is fixture-closure resolution (R§3). The
conclusion is not "skip collection jest-style" — it is "keep the import, delete the node tree".*

---

## 1. What collection produces

A flat `list[TestRecord]`. No tree, no parent pointers, no per-path hook proxies, no `Package`
nodes, no `Session`/`Module`/`Class` node objects.

```python
@dataclass(frozen=True, slots=True)
class TestRecord:
    id: str                      # "tests/api/test_users.py::test_create[admin]"
    index: int                   # logical order position; stable, dense, 0-based
    path: Path                   # relative to rootdir
    lineno: int                  # definition line, for editor links and ordering
    qualname: str
    func: Callable               # the raw function; velox never wraps it
    params: Mapping[str, object] | None
    marks: MarkSet               # frozen: skip/skipif/xfail/tags/timeout/solo/isolated
    plan: ResolutionPlan         # from the DI graph — see 04
    exclusive: frozenset[str]    # transitive exclusive tokens — see 06
```

Everything the runner needs is in this record. Nothing requires walking anywhere. `index` is
assigned once, after sorting, and is the definition of logical order (I2).

## 2. Discovery

One `os.scandir` walk from each root in `testpaths`, with:

- a compiled filename filter from `test_file_patterns` (default `test_*.py`, `*_test.py`);
- a fixed ignore set for directory names (`.git`, `.venv`, `__pycache__`, `node_modules`, caches,
  `build`, `dist`), extended from config;
- symlink loop protection by `(st_dev, st_ino)`;
- no `__init__.py` requirement, and no consultation of `sys.path`.

If `PATHS` contains explicit files or ids, the walk is skipped for those entries. Directory results
are sorted by name so the walk itself is deterministic before any sorting pass.

## 3. Import

**importlib only, one position, no alternatives** (R§3). For each file:

1. Compute a unique module name from the path relative to rootdir:
   `velox_tests.<dotted.relpath.without.suffix>` with non-identifier characters escaped.
   Path-derived names mean two `test_utils.py` files in different directories never collide, which
   is the entire content of pytest's `ImportPathMismatchError`.
2. `importlib.util.spec_from_file_location` → `module_from_spec` → insert into `sys.modules` →
   `exec_module`. The assertion-rewriting meta-path finder ([07](07-assertions.md)) intercepts at
   step 2 by matching the module name prefix and the path.
3. An exception during `exec_module` becomes a **collection error** attributed to that file: it is
   reported like a failed test (with a full traceback), the file contributes zero tests, and the run
   continues. Exit code `1`, or `2` under `--strict-collect`.

This deletes, versus pytest: `Package` collection, `sys.path` insertion (`prepend`/`append`/
`importlib` modes), namespace-package configuration, `consider_namespace_packages`, and
`ImportPathMismatchError` — pytest's worst legacy tax.

**Consequence to document:** the code under test must be importable on its own (installed, or on
`PYTHONPATH`). For `uv`/`pip install -e .` projects this is already true. For the "flat script
directory with no package" layout it is not, and the error message must say exactly that with the
fix.

## 4. Building records

Per imported module, in module-definition order:

1. Iterate `vars(module)` filtered by name pattern and `getattr(obj, "__module__") == module.__name__`
   (so imported helpers named `test_*` are not collected twice), plus `Test*` classes.
2. Sort by `func.__code__.co_firstlineno` — definition order, not dict order, so a refactor that
   reorders imports doesn't reorder tests.
3. Expand `@velox.parametrize` into one record per callspec. Parametrization is genuinely cheap in
   pytest (~24 µs/item) and stays cheap here because the resolution plan is computed once per
   function and shared across callspecs (R§3).
4. Read the injection plan from `__defaults__`/`__kwdefaults__` (no `inspect.signature`, no
   annotation evaluation — see [01](01-public-api.md) design rule 3) and hand it to the DI graph
   builder ([04](04-dependency-injection.md)).
5. Apply `-k`/`-m`/`--deselect` as **pure predicates over the single record**. No global item list
   is needed for filtering; pytest's `-k`/`-m` implementation is a 353-LOC expression parser and
   that part is worth reimplementing faithfully (R§3).

Then: assign `index` over the concatenation of per-file records sorted by relative path, and run
static DI validation over the whole set before dispatching anything.

## 5. What is deliberately not built

From R§3, each of these is a decision, not an omission:

| Not built | Because |
|---|---|
| Node tree, parent pointers, `iter_parents` | 250k calls on the benchmark suite, serving only fixture visibility and hook proxying — both gone. |
| `FSHookProxy`, per-directory hook resolution | No hooks. |
| `conftest.py` hierarchies | One explicit fixture module, imported like normal Python. |
| `pytest_collection_modifyitems` | No hooks; selection is CLI-level and total. |
| `reorder_items` | It exists to serve a *sequential* scope cache; under concurrency it concentrates contention rather than relieving it. |
| unittest / doctest collection | Out of scope. |
| `getfixturevalue` (dynamic lookup) | Its existence is precisely what forces pytest to treat every fixture closure as incomplete, and it would break static footprints (§06). |
| Two node objects per test (`FunctionDefinition` + `Function`) | ~40% of collection cost. |

## 6. The persistent collection cache

`.pytest_cache` does **not** cache collection — `--lf` re-collects everything and then filters
(R§3). velox builds the index pytest never did.

**Store:** `.velox/collect.json` (or msgpack), mapping `relpath → {mtime_ns, size, ids: [...], lines: [...]}`.
Entries are validated by `(mtime_ns, size)`; a mismatch invalidates that file only. A version tag
covers the velox version, the config keys that affect collection, and the Python version.

**Buys:**
- `--lf` / `--ff` without importing unchanged files — the failure-first ordering that makes a red
  suite fast to iterate on.
- Instant `--collect-only` and accurate test counts *before any import*, so the progress bar has a
  denominator immediately rather than after collection (jest cannot do this at all).
- Editor integrations and `--co-json` at near-zero cost.

**Correctness rule:** the cache is only ever used to *order* and to *predict counts*, never to skip
running a test that was selected. A stale cache can make the footer's denominator wrong for a few
hundred milliseconds; it can never change which tests run. That asymmetry is what keeps it safe.

## 7. Collection is always complete before dispatch

**Decided:** velox collects the entire suite — walk, import, expand, validate — before dispatching
the first test. No streaming collection, not in the MVP and not on the roadmap.

The cost is bounded and small: importing the test modules is only ~8% of pytest's collection time
(R§3), and velox has deleted the other 92%, so full collection on a 5000-test suite should land in
the low hundreds of milliseconds — comparable to the loop startup it overlaps with anyway.

What it buys is worth more than that latency:

- `--maxfail` is defined over a closed, fully-known logical order;
- **static DI validation sees the whole graph** before anything runs, so a scope-compatibility error
  fails in 200 ms instead of 40 s into the run ([04](04-dependency-injection.md) §2);
- the scheduler's exclusive-set admission and aging operate over a complete ready queue, which is
  what makes physical order a pure function of the test set ([06](06-scheduling-and-determinism.md));
- the progress denominator is exact from the first frame, with or without the cache.

The jest trade-off ("total count unknown until files load") is therefore **not** taken. That was a
concession streaming demanded, and velox isn't streaming.

## 8. MVP

Walk + filter, importlib import with unique names, per-module record building, parametrize
expansion, `-k`/`-m`/`--deselect`, logical ordering and `index` assignment, collection errors as
reported failures, `--collect-only`. Collect fully before dispatch.

## 9. Roadmap

- Persistent collection cache; `--lf`/`--ff`; `--co-json`.
- Watch mode (`--watch`), which is the collection cache plus a file watcher plus `--lf` ordering —
  cheap once the cache exists, and the feature jest users miss most.
- Parallel import behind a flag, only if measurement justifies it ([02](02-cli-and-config.md) §6).

## 10. Open questions

- **Q9** — Should `Test*` class collection require the class to be free of `__init__`, or silently
  skip classes with one (pytest warns)? Proposed: warn and skip, matching pytest, because dataclass
  containers named `TestCase*` are common in helper modules.
- **Q10** — Cache invalidation by `(mtime_ns, size)` vs content hash. mtime is cheaper and matches
  the `.pyc` convention already used by the rewriter; content hashing is correct under
  checkout-shuffling CI. Proposed: mtime+size by default, `--cache-hash` for CI.
