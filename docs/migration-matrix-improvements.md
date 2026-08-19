# Migration matrix improvements

The [support matrix](../velox-migrate/velox_migrate/matrix.py) classifies every pytest construct that the migration tool encounters. Each row carries a `VXnnn` code and a `disposition` — one of `MECHANICAL` (auto-convert, no review needed), `MARKER` (convert but flag for review), `REFUSED` (do not convert), `UNSUPPORTED` (impossible without a new subsystem), or `HAZARD` (concurrent run changes the behavior).

A construct is `REFUSED` or `UNSUPPORTED` for one of three reasons:

1. **Velox lacks a feature.** The pattern exists in pytest; velox could support it with a localized addition.
2. **Velox's concurrency model makes it impossible.** The construct relies on serial execution, shared process state, or hooking into pytest's collection/reporting.
3. **The construct requires algorithmic work.** A smarter codemod or a deliberate rewrite would unlock it.

This document focuses on category 1: features velox could add to unlock matrix rows that are currently stuck for lack of them, not for architectural reasons.

## Candidates for velox changes

| Code | Current | Feature | Promoted to |
|---|---|---|---|
| VX210 | REFUSED | Add a callable form to `velox.raises`: `raises(E, func, *args, **kwargs)` — call `func(*args, **kwargs)` inside the existing context manager. pytest has this form; velox doesn't. | MECHANICAL |
| VX213 | UNSUPPORTED | Extend `velox.approx` to walk sequences and mappings elementwise, matching pytest's own behavior. Today it accepts scalars only. | MECHANICAL |
| VX206 | UNSUPPORTED | Add `.text`, `.record_tuples`, and `.clear()` to `velox.log_records`. These format or manipulate captured logs the same way `caplog` does. | MECHANICAL |
| VX105 | REFUSED | Add a `condition=` parameter to `@velox.xfail`, mirroring `@velox.skipif`'s own conditional form. | MECHANICAL |
| VX102 | REFUSED | Let parametrize cases carry per-case marks — e.g., a `velox.case(value, marks=...)` wrapper `@velox.parametrize` recognizes. Pytest allows `pytest.param(..., marks=...)` per case. | MECHANICAL |
| VX208 | REFUSED | Ship a `py.path.local`-compatible wrapper for `tmp_path`, so `tmpdir`/`tmpdir_factory` bodies (which use `.join`, `.strpath`, division) can migrate. pytest itself carries this legacy shim. | MECHANICAL |
| VX014 | REFUSED | Give a fixture body an imperative teardown-registration call that runs conditionally and can register multiple times. Today only unconditional `yield` teardowns work. | MECHANICAL or MARKER |
| VX214 | REFUSED | Add imperative skip/fail functions (`velox.fail(msg)`, a runtime exception) alongside the existing decorators, so `pytest.skip()` / `pytest.fail()` statements in bodies have a real target. | MECHANICAL |

## Why these were left off the candidate list

- **VX006, VX012, VX027, VX028, VX029, VX030** — These involve dynamic or computed fixture lookup and fixture-override specialization. The fixes would require more implicit dependency-injection machinery. [[feedback-minimize-implicit-di]] already flags this territory as deferred, pending a deliberate architectural call.
- **VX016, VX020, VX021, VX022, VX023, VX203, VX216, VX307, VX308** — These need new subsystems (plugin CLI surface, unittest lifecycle, doctest collection, hook protocol, file-descriptor capture, process-global warning filters under concurrency). Not "simple" feature additions.
- **VX113** — Double marks (e.g., two `@velox.skip` on one test) are deliberate validation errors, not gaps.
- **VX019, VX215** — The matrix's own `action` text describes a tool rewrite the migration tool could perform today, not a velox gap.
- **Hazard block (VX401–VX416)** — These exist because velox runs tests concurrently. "Fixing" them would mean abandoning concurrency.

## Next steps

The shortlist above is ordered by implementation cost and audit impact: VX210, VX213, VX206 are the cheapest wins and would unlock cases commonly seen in real suites. Each row carries an `action` in the matrix itself; promoting it moves that action from a "manual rewrite" task to "the migration tool can do it."

Consider prototyping the cheapest ones as velox features and re-running the migration tool on a real OSS suite to measure the impact on the final report.
