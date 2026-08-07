# 02 — CLI and Configuration

*Small surface, one argv parse, no plugin-injected options. Pytest's startup costs 130–300 ms partly
because plugins add options mid-flight and force **three** argv parses plus an entry-point scan that
is O(installed plugins) whether used or not (R§3). velox has one parse because it has no plugins.*

---

## 1. Invocation

```
velox [PATHS...] [options]
```

`PATHS` are files, directories, or test ids (`tests/api/test_users.py::test_create[admin]`).
Default: the `testpaths` from config, else the rootdir. There is one entry point (`velox`) and
`python -m velox` as an alias.

## 2. Options

Grouped as they appear in `--help`. Anything marked † is roadmap, listed here so the namespace is
reserved and the codegen can plan for it.

**Selection**

| Option | Meaning |
|---|---|
| `-k EXPR` | Keyword expression over test id (pytest grammar: `and`/`or`/`not`, substring terms). |
| `-m EXPR` | Tag expression over `@velox.tag` values (same grammar). |
| `--deselect ID` | Repeatable; exact id or path prefix. |
| `--lf` / `--ff` † | Last-failed / failed-first, backed by the collection cache ([03](03-discovery-and-collection.md)). |

**Execution**

| Option | Default | Meaning |
|---|---|---|
| `--concurrency N` | `16` | Max tests in flight. `1` = fully serial (the first debugging step). |
| `--timeout SECONDS` | `300` | Per-test budget; `@velox.timeout` overrides. `0` disables. |
| `-x`, `--maxfail N` | off | Stop dispatching after N failures; in-flight tests are cancelled and reported `interrupted` ([06](06-scheduling-and-determinism.md)). |
| `--seed N` | `0` | Seeds scheduler tiebreaks. Physical order only; output is unaffected (I2). |
| `--serial` | off | Alias for `--concurrency=1`, plus enables the serial-only features (real fd capture, per-test warning filters). |
| `--isolated-all` † | off | Run every test in a subprocess. The escape hatch for a suite velox is diagnosing. |
| `--loop {auto,asyncio,uvloop}` | `auto` | `auto` = uvloop if importable. |

**Assertions**

| Option | Default | Meaning |
|---|---|---|
| `--assert {rewrite,plain}` | `rewrite` | `plain` skips the import hook entirely; PEP 657 carets still render ([07](07-assertions.md)). |
| `--rewrite-cache DIR` | platform cache dir | Where rewritten `.pyc`s go. Unwritable ⇒ warn + fall back to `plain`, never silently pay 4.6× (R§2). |

**Output**

| Option | Default | Meaning |
|---|---|---|
| `-v` / `-q` | normal | One line per test / one char per file. |
| `-s`, `--capture {sink,no}` | `sink` | `no` passes output straight through, prefixed with the test id (or forces serial if `--prefix-output=no`). |
| `--durations N` | `10` | Slowest N tests. More load-bearing here than in pytest: it is how users tune `--concurrency`. |
| `--stream-failures` † | off | Print failure detail as it happens instead of at the end (long runs). Breaks byte-identical output by construction; documented as such. |
| `--color {auto,always,never}` | `auto` | Honors `NO_COLOR`, `FORCE_COLOR`, `CI`. |
| `--junit-xml PATH` † | — | JUnit XML output ([10](10-reporting.md)). |
| `--report-json PATH` † | — | Full serialized run. |

**Diagnostics**

| Option | Default | Meaning |
|---|---|---|
| `--watchdog-threshold SECONDS` | `1.0` | Loop-stall detection threshold ([11](11-runtime-safety.md)). |
| `--watchdog {warn,fail,off}` | `warn` | What a stall does. `fail` marks the blocking test failed. |
| `--collect-only` | off | Print ids in logical order and exit `0`. |
| `--co-json` † | off | Machine-readable collection, for editor integrations. |

## 3. Configuration file

One file, `pyproject.toml`, table `[tool.velox]`. No `velox.ini`, no `setup.cfg`, no per-directory
config, no config inheritance. Rootdir = the directory containing the `pyproject.toml` that declares
`[tool.velox]`, searched upward from the common ancestor of `PATHS`; if none is found, the common
ancestor itself is the rootdir and defaults apply.

```toml
[tool.velox]
testpaths = ["tests"]
concurrency = 16
timeout = 300
test_file_patterns = ["test_*.py", "*_test.py"]
ignore = [".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache", "build", "dist"]
basetemp_retention = 3          # keep N previous session temp roots
env = { ENVIRONMENT = "test" }  # set before any test module import
watchdog_threshold = 1.0
```

Precedence: **CLI > environment (`VELOX_*`) > `[tool.velox]` > built-in defaults.** Every option has
exactly one name in all three places (`--concurrency` / `VELOX_CONCURRENCY` / `concurrency`).
Unknown keys in `[tool.velox]` are an error, not a warning — a typo'd config key that silently does
nothing is a footgun the ecosystem has taught users to expect, and we don't have to.

## 4. Exit codes

Adopted verbatim from pytest, because every CI script branches on them (R§5):

| Code | Meaning |
|---|---|
| `0` | All tests passed (or were skipped/xfailed). |
| `1` | Some tests failed. |
| `2` | Interrupted — Ctrl-C, `--maxfail` reached, internal cancellation. |
| `3` | Internal error. |
| `4` | Usage error — bad CLI, bad config, unknown option. |
| `5` | **No tests collected.** Load-bearing: catches "the selector matched nothing and CI went green". |

Static DI validation failures ([04](04-dependency-injection.md)) and import errors during collection
exit `3` if they are velox's fault and `1` if they are the suite's — an unimportable test module is
reported as a **collection error** attributed to that file, and the rest of the suite still runs.
`--strict-collect` † makes any collection error exit `2` immediately.

## 5. Environment interaction

- **Reads:** `NO_COLOR`, `FORCE_COLOR`, `CI`, `VELOX_*`, `PYTHONHASHSEED` (recorded in the report
  header for reproducibility), `TERM`.
- **Writes:** `VELOX_TEST_ID` is *not* set — pytest's `PYTEST_CURRENT_TEST` is meaningless with N
  tests in flight. The concurrent replacement is the watchdog's in-flight file
  ([11](11-runtime-safety.md)). `env` from config is applied before the first test module import.
- **Does not touch `sys.path`.** Import is importlib-only with path-derived module names (R§3);
  this single decision deletes `Package`, `ImportPathMismatchError`, and the `__init__.py`
  requirement. Users needing their package importable install it (`uv pip install -e .`), which is
  already the norm.

## 6. Startup sequence and budget

Budget: **< 50 ms from process start to first test dispatched** (I7), CI-checked with a
regression gate on a trivial suite.

```
1. parse argv (stdlib argparse, one pass, no plugin hooks)   ~2 ms
2. locate rootdir, read pyproject.toml                        ~3 ms
3. install assertion meta-path finder                         ~1 ms
4. install capture routers, log handler, excepthooks          ~1 ms
5. start loop (uvloop if available) + watchdog thread         ~5 ms
6. scandir walk + name filter                                 ~5 ms / 1k files
7. import modules (concurrently? no — see below) + build records
8. static DI validation
9. dispatch
```

Steps 1–6 are the fixed cost; 7 scales with the suite and is dominated by the user's own imports.
Module import happens on the loop but **sequentially** in v1: imports mutate `sys.modules`, run
arbitrary code, and are not reentrancy-safe; the measured win from parallelising them (~8% of
collection cost, R§3) does not justify the class of bug. Revisit only with data.

Lazy-import discipline: `rich`, the JUnit/JSON emitters, the traceback formatter, and the migration
tooling are imported on first use, never at startup.

## 7. MVP

Selection (`-k`, `-m`, `--deselect`, paths and ids), `--concurrency`, `--timeout`, `-x/--maxfail`,
`--seed`, `--serial`, `--assert`, `--rewrite-cache`, `-v/-q`, `-s`, `--durations`, `--color`,
`--collect-only`, watchdog options, `[tool.velox]` with the keys above, all six exit codes.

## 8. Roadmap

`--lf`/`--ff` (needs the collection cache), `--junit-xml`, `--report-json`, `--co-json`,
`--stream-failures`, `--isolated-all`, `--strict-collect`, shell completion, and a `velox migrate`
subcommand ([12](12-migration.md)).

## 9. Open questions

- **Q5** — Fixed `--concurrency=16` default vs auto-calibration from the first N tests. Fixed is
  proposed: predictable, explainable, and `--durations` plus the footer give users the signal to
  tune it themselves. Auto-tuning would make wall-clock non-reproducible across machines, which
  undercuts the determinism story even though it doesn't violate I2 literally.
- **Q8** — Should `--serial` silently enable serial-only capabilities (fd capture, per-test warning
  filters), or require opting into each? Silent enabling means a test can pass serial and fail
  concurrent, which is exactly the confusion we want to avoid — but requiring three flags to debug
  one test is hostile. Proposed: enable them, and have the reporter state in its header which
  serial-only behaviors are active.
