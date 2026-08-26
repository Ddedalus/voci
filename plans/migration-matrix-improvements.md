# Migration matrix improvements

The [support matrix](../velox-migrate/velox_migrate/matrix.py) classifies every pytest construct that the migration tool encounters. Each row carries a `VXnnn` code and a `disposition` — one of `MECHANICAL` (auto-convert, no review needed), `MARKER` (convert but flag for review), `REFUSED` (do not convert), `UNSUPPORTED` (impossible without a new subsystem), or `HAZARD` (concurrent run changes the behavior).

A construct is `REFUSED` or `UNSUPPORTED` for one of three reasons:

1. **Velox lacks a feature.** The pattern exists in pytest; velox could support it with a localized addition.
2. **Velox's concurrency model makes it impossible.** The construct relies on serial execution, shared process state, or hooking into pytest's collection/reporting.
3. **The construct requires algorithmic work.** A smarter codemod or a deliberate rewrite would unlock it.

This document focuses on category 1: features velox could add to unlock matrix rows that are currently stuck for lack of them, not for architectural reasons.

## Done

VX210, VX213, VX206, VX105, VX102, VX208 and VX214 are done — promoted to `MECHANICAL` in
[the matrix](../velox-migrate/velox_migrate/matrix.py) 

VX208's `LegacyPath` (`velox/_builtins/fixtures.py`) wraps a `pathlib.Path` with `.join`,
`.strpath`, `.write`, `.mkdir` and `/` division — the shape pytest's own `tmpdir` shim carries —

## Abandoned for now

Too much fuss for rare syntax:

| Code | Current | Feature | Promoted to |
|---|---|---|---|
| VX014 | REFUSED | Give a fixture body an imperative teardown-registration call that runs conditionally and can register multiple times. Today only unconditional `yield` teardowns work. | MECHANICAL or MARKER |

## Why these were left off the candidate list

- **VX006, VX012, VX027, VX028, VX029, VX030** — These involve dynamic or computed fixture lookup and fixture-override specialization. The fixes would require more implicit dependency-injection machinery. [[feedback-minimize-implicit-di]] already flags this territory as deferred, pending a deliberate architectural call.
- **VX016, VX020, VX021, VX022, VX023, VX203, VX216, VX308** — These need new subsystems (plugin CLI surface, unittest lifecycle, doctest collection, hook protocol, file-descriptor capture, recorded-warning assertions). Not "simple" feature additions.
- **VX113** — Double marks (e.g., two `@velox.skip` on one test) are deliberate validation errors, not gaps.
- **VX019, VX215** — The matrix's own `action` text describes a tool rewrite the migration tool could perform today, not a velox gap.
- **Hazard block (VX401–VX416)** — These exist because velox runs tests concurrently. "Fixing" them would mean abandoning concurrency.
