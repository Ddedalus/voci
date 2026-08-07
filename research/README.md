# research/ — artifacts behind RESEARCH.md

Working code and raw data produced during the 2026-08-06 pytest research
(recovered from the research agents' scratchpad). Everything here backs specific
claims in [../RESEARCH.md](../RESEARCH.md); paths inside the scripts point at the
pytest clone in `../pytest`, so they run from a checkout of this repo as-is
(CPython 3.11+, no third-party deps needed unless noted).

## The assertion-rewriter extraction (RESEARCH.md §2)

- **`extract.py`** — the proof that lifting pytest's rewriter is a ~30-line diff.
  Reads `../pytest/src/_pytest/assertion/rewrite.py`, applies the 7 recorded edits
  (import shim, injected helper-module name, warning plumbing), and writes
  `velox_rewrite.py`. Each edit is labeled; this file *is* the coupling-point list.
- **`velox_rewrite.py`** — the resulting standalone rewriter (generated; regenerate
  with `python extract.py` after pulling a newer pytest).
- **`_velox_shim.py`** — the ~130-LOC support shim: `fnmatch_ex`/`absolutepath`,
  a minimal `Config`, a 2-method `Session` protocol, `format_explanation` helpers,
  and the namespace holding the three globals that must become `ContextVar`s in
  velox proper.

## Benchmarks

- **`run_bench.py`** — end-to-end demo + micro-benchmarks of the standalone
  rewriter: prints the transformed source for sample asserts, verifies diffs,
  line-number preservation, and `await`-in-assert, then times passing-assert
  overhead (the +4..37 ns table).
- **`run_bench2.py`** — import-time phase breakdown on
  `../pytest/testing/test_assertion.py` (3092 LOC, 217 asserts): parse vs rewrite
  vs compile vs marshal-load (the 4.6x-cold / 154x-warm numbers).
- **`bench_rewrite.py`** — runs the *in-tree* pytest rewriter directly from
  `../pytest/src` (stubs `_pytest._version` and pygments) for comparison against
  the extracted one.
- **`pep657.py`** + **`_pep657_sample.py`** — demo of what PEP 657 caret spans give
  for free on an un-rewritten `AssertionError` (the ~50-LOC fallback floor).
- **`gen_suite.py`** — regenerates the synthetic suites for the collection/run
  benchmarks (200 files x 25 tests + a 5000-case parametrized suite); usage and
  the commands to reproduce the §3 numbers are in its docstring. The suites are
  generated on demand rather than checked in.

## profiles/

Raw cProfile output from the collection/run analysis (RESEARCH.md §3), taken on
the generated 5000-test suite:

- **`prof.txt`** — `pytest --co` ordered by internal time.
- **`prof2.txt`** — same run ordered by cumulative time (the source of the
  per-phase attribution table: `Function.__init__` ~40%, fixture closure ~26%,
  imports ~8%).
- **`prof3.txt`** — full test run ordered by cumulative time (the ~600 us/test
  protocol-overhead breakdown; hook dispatch dominates, fixtures ~13%).
