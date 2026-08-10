# 09 — Capture: stdout/stderr, logging, temp paths

*Pytest's default capture is `os.dup2` on fds 1/2 into a shared temp file, with a single global slot
and suspend/resume state asserts. That is **structurally impossible** under concurrency — the byte
stream contains no attribution information, even in principle (R§7). velox routes by context
instead, and installs exactly once.*

---

## 1. Output routing

```
sys.stdout = Router(real_stdout)     # installed once, at session start, never swapped
sys.stderr = Router(real_stderr)

class Router:
    def write(self, s):
        sink = _current_sink.get()   # ContextVar
        (sink or self._session_sink).write(s)
```

- One installation for the whole session; no per-phase add/remove, no suspend/resume state machine,
  no asserts about who holds the slot.
- **Task context propagation does the attribution**: everything the test awaits or spawns writes to
  the test's `Sink`, because the test task's context carries the ContextVar.
- Output produced outside any test's context (import time, session-scoped fixture construction
  before the first waiter, background library threads) goes to the **session sink** and is reported
  in an "unattributed output" section.
- A `Sink` is an in-memory buffer with a size cap (default 4 MiB/test, config `capture_limit`) that
  switches to head+tail truncation with a marker, so a runaway `print` in a loop cannot OOM the run.

`-s` / `--capture=no` passes writes straight through to the real stream, **prefixed with the test id
per line** (so concurrent output stays readable), or forces serial if the user asks for unprefixed
output.

## 2. Logging

**One root logging handler for the whole session**, whose `emit` writes to the current sink. This is
strictly less code than pytest's add-and-remove-handlers-per-phase dance, and it is race-free by
construction (R§7).

- Structured `LogRecord`s are retained per test (not just formatted text) for the `caplog`
  equivalent, `velox.log_records`.
- `velox.log_records.set_level(logging.DEBUG, logger="myapp")` is a context manager. **Logger levels
  are process-global**, so raising a level affects concurrent tests: the effect is only that other
  tests may capture *more* records than they otherwise would, which is benign for assertions of the
  form "this record is present" and hazardous for "no records were emitted". Documented; a strict
  mode escalates `set_level` to solo (roadmap).
- Formatting is applied at report time, not at emit time — cheaper, and lets `-v` change format
  after the fact. One consequence: unlike pytest's `LogCaptureHandler` (which calls `self.format
  (record)` in `emit`, setting `record.message` as a side effect), velox's handler never formats a
  record onto a stream, so a raw `LogRecord` off `velox.log_records.records` has no `.message`
  attribute set. Use `velox.log_records.messages` (`record.getMessage()`, already applied) for
  text; `.records` is for level/name/exc_info and similar structured fields.

## 3. Threads

The attribution story, precisely (R§7):

| Path | Attributed? | Why |
|---|---|---|
| `await` anything | Yes | Same task, same context |
| Task spawned in the test's `TaskGroup` | Yes | Context inherited at task creation |
| `asyncio.to_thread(...)` | Yes | Stdlib does `copy_context().run` by design |
| `loop.run_in_executor(None, ...)` | **Yes** | velox installs a **context-propagating default executor**: `loop.set_default_executor(...)` with a `submit` that wraps the callable in `ctx.run`. Correct because `run_in_executor` is invoked during the awaiting task's step, so submit-time context *is* the test's. |
| SQLAlchemy's greenlet bridge | Yes | Same thread, same context |
| User-created `ThreadPoolExecutor` | No | velox never sees the submit |
| Raw `threading.Thread` | No | Same |
| Direct fd writes from C extensions / subprocesses | No | Bypasses `sys.stdout` entirely |

The last three land in the unattributed section. Real fd-level capture is available only under
`--serial` / `--isolated`, where a single owner of fds 1/2 exists.

## 4. Warnings

Covered in [11](11-runtime-safety.md) §3, but noted here because the attribution mechanism is
shared: a `showwarning` shim attributes each warning via the capture ContextVar.

## 5. `tmp_path`

Allocate deterministically: `basetemp/<sanitized-test-id>`. **Uniqueness by construction** —
pytest's scan-and-retry numbering (`test_foo0`, `test_foo1`, …) is a serial-era artifact and a race
under concurrency (R§7). Keep the parts that are good:

- a numbered session root (`/tmp/velox-of-<user>/velox-<n>/`),
- a retention policy (`basetemp_retention`, default 3 previous roots),
- `--basetemp` to override, with the documented "this directory is cleared" warning.

Sanitization must be injective enough to avoid collisions between distinct ids (parametrized ids
with `/` or spaces) — hash-suffix any id that needs escaping.

## 6. What the reporter does with captured output

Captured stdout/stderr and log records are attached to the `TestResult` and shown **only for
failing tests** (plus `-v` on demand), in the failure block, in logical order like everything else
([10](10-reporting.md)). Passing tests' captured output is dropped as soon as the result is
finalized, so memory is bounded by concurrency, not by suite size.

## 7. MVP

Router install for stdout/stderr; session sink + per-test sinks with size caps; the single root log
handler with structured record retention; the context-propagating default executor; `tmp_path` /
`tmp_path_factory`; the unattributed-output section; `-s` with id prefixes.

## 8. Roadmap

- Real fd capture under `--serial`/`--isolated` (`dup2` onto a per-run temp file) for suites that
  need C-extension output.
- `caplog`-parity API surface (`at_level`, `record_tuples`, `messages`, per-logger filtering).
- Strict mode: `set_level` and per-test warning filters escalate to solo rather than being
  best-effort.
- Streaming capture to disk for tests that legitimately produce megabytes.

## 9. Open questions

- **Q4** — Unattributable fd-level output: tee it into a session-level section (proposed) or detect
  and fail? Teeing loses nothing and keeps velox usable with C extensions; failing would be more
  honest about "we cannot attribute this" but breaks common libraries for no gain.
- **Q19** — Should captured output for *passing* tests be retained under `--report-json`? It makes
  the report complete but unbounded. Proposed: drop by default, `--capture-retain=all` to keep.
