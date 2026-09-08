# 10 — Tracebacks, Reporter, Machine-Readable Output

*Two decisions from R§7: **do not port `_pytest/_code`** (3000 LOC whose judgment lives in
config-coupled layers — stdlib 3.11+ plus rich covers it, port exactly two ideas), and **jest got
the reporter right**, with xdist showing the failure mode to avoid.*

---

## 1. Tracebacks

Built on `traceback.TracebackException` (3.11+): PEP 657 caret positions, `ExceptionGroup`
rendering, and `__cause__`/`__context__` chains all come free. On top of that, exactly four
behaviors:

1. **`__tracebackhide__`** — one dict lookup per frame, drops the frame. Essential the moment users
   write assertion helpers, which every real suite does.
2. **Cut to the test function** — drop all frames above the test, identified by its code object's
   filename and `firstlineno`. Two lines of code, disproportionate payoff (R§7).
3. **Suppress runner frames** — `asyncio/`, `anyio/`, and `voci/` internal frames are hidden by
   default, or every traceback drags in `Task.__step`, `TaskGroup.__aexit__`, and the envelope. This
   is async-specific and non-negotiable; `--full-trace` disables it.
4. **Filter rewriter temps** — `@py_assert*` locals never appear in a locals display
   ([07](07-assertions.md) §6).

Locals display (`--showlocals`) uses the vendored `saferepr` with the same truncation policy as the
explanation engine.

### `FailureRepr`

Failure representations are **serializable dataclasses from day one** (I4) and **built lazily, only
on failure** (I5):

```python
@dataclass(frozen=True, slots=True)
class FailureRepr:
    exc_type: str
    message: str                       # the rendered explanation for AssertionError
    frames: list[FrameRepr]            # path, lineno, colno span, function, source line, locals?
    chain: list[FailureRepr]           # cause/context
    group: list[FailureRepr]           # ExceptionGroup members
    section: dict[str, str]            # captured out/err/log, teardown notes
```

Rendering (ANSI, plain, JUnit, GitHub annotation) is a pure function of this structure. That is what
makes `--isolated` and `--report-json` nearly free, and it is the thing pytest had to retrofit under
xdist pressure (R§8.8).

## 2. Terminal reporter

Two regions on a tty:

**Scrollback — atomic per file.** A file's block is flushed when all of that file's tests have
finished. Ordering *between* blocks is by completion; the content *within* a block is by logical
order, so a block is internally coherent and never interleaved with another file's output. This is
jest's model and it is the right one; xdist's failure mode is exactly the absence of it — interleaved
per-test lines from N workers with no coherent unit.

```
PASS  tests/api/test_users.py          12 tests   0.84s
FAIL  tests/api/test_billing.py         8 tests   2.10s   (1 failed)
```

**Live footer (~10 Hz), rewritten in place:**

```
  Running 14/16 · 312/1204 done · 2 failed · 18.4s
    ▸ tests/api/test_checkout.py::test_full_flow            4.2s
    ▸ tests/db/test_migrations.py::test_upgrade_all         3.9s
    ▸ … 12 more
```

One line per in-flight test with elapsed time. This **doubles as the hang-diagnosis UI** and is
something serial pytest structurally cannot offer — when a suite stalls, the footer already names
the culprit before the watchdog fires.

**At the end**, in logical order (I2):

- failure details, one block per failing test (traceback + captured sections);
- the **short test summary** — `FAILED tests/api/test_billing.py::test_refund - AssertionError: ...` —
  kept verbatim from pytest because it is the most-copied line in pytest's output;
- `--durations=N`;
- the final line: **wall clock vs Σ durations**, e.g.
  `1204 tests · 42 failed · 18.4s wall (Σ 214.7s, 11.7× concurrency)`. This is the proof-of-value
  metric and it belongs on every run.

**Non-tty / CI:** no ANSI, no footer, one line per file as it completes, all detail at the end.
Honor `NO_COLOR`, `FORCE_COLOR`, `CI`.

**Header** states the run's configuration facts that change semantics: concurrency, seed, loop
implementation, assertion mode (and whether it fell back), and any serial-only behaviors in effect
(Q8).

## 3. Reporting the concurrency-specific facts

Things that don't exist in pytest and must be visible:

- number of tests that ran **solo**, and the wall-clock the suite spent drained for them;
- exclusive-token contention summary (top tokens by time held) under `-v`;
- unattributed output section, if non-empty;
- teardown errors section, for wide-scope fixtures whose failures aren't attributable to one test;
- watchdog stalls ([11](11-runtime-safety.md)).

## 4. Machine-readable output

**JUnit XML** (~150 useful LOC): testcases in logical order; `<testsuite time>` carries **honest wall
clock**. CI dashboards sum per-test times and will otherwise report the fast suite as slow —
document the discrepancy explicitly, since it will otherwise be reported as a voci bug.

**GitHub Actions annotations**: `::error file=…,line=…,col=…::message`. Cheap, high-value, and the
column comes free from PEP 657 positions.

**`--report-json`**: the whole run — config header, per-test `TestResult` including timings and
`FailureRepr`, plus scheduler facts (dispatch order, token holds). Enables an external Gantt view,
flake tracking, and voci's own integration tests.

## 5. Colors and formatting dependency

`rich` for terminal rendering, imported lazily and only when a tty is detected. Non-tty output is
built with plain string formatting and no dependency, so CI never pays the import. If `rich` proves
to be a startup-budget problem (I7) even lazily, the fallback is a ~150-LOC internal renderer —
noted, not planned.

## 6. MVP

Traceback rendering with the four behaviors; `FailureRepr`; tty reporter with per-file blocks, live
footer, failure details and short summary in logical order; the wall-vs-Σ final line; non-tty mode;
`--durations`; solo/unattributed/teardown-error sections; header.

## 7. Roadmap

- JUnit XML, GitHub annotations, `--report-json`.
- `--stream-failures` for long runs (documented as breaking byte-identical output).
- An HTML/Gantt view of the concurrency timeline from the JSON report — this is the artifact that
  *shows* why voci is fast, and it is nearly free given the timing data.
- Flake detection (`--rerun-failed=N`) reported as a distinct outcome rather than hidden.
- Diff-quality improvements in the explanation engine ([07](07-assertions.md)).

## 8. Open questions

- **Q20** — Should per-file blocks flush on file completion (proposed, jest-style) or in strict
  logical file order (which would delay a fast file behind a slow earlier one, but make even the
  scrollback byte-identical)? Proposed is completion order, with the end-of-run sections carrying
  the determinism guarantee. A `--ordered-scrollback` flag can offer the other trade.
- **Q21** — `rich` as a hard dependency vs vendoring a minimal renderer. Proposed: hard dependency,
  lazily imported, revisited only if the startup budget is threatened.
