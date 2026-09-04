# `_warnings.py` — warning filters

See [rationale.md](../rationale.md) for the index.

**Filters are evaluated in velox's own shim, not written into `warnings.filters`.** That list is
process-global and CPython consults it before `showwarning` is ever reached, so a per-test filter
installed there governs every test dispatched alongside, and `catch_warnings`' save/restore of it
races every concurrent test's own. The way out is to stop asking CPython to decide: the run-wide
filter is set to `always`, which makes every warning reach the shim undropped, and the shim reads
the filters of whichever test raised it off a `ContextVar`. Nothing about the mechanism is a
concession to a Python version — a filter that says `ignore` on one test and `error` on the next
means exactly that, at any concurrency.

**The `error` action raises out of `warnings.warn(...)` itself.** An exception raised inside
`showwarning` propagates through the call that warned, so the failure lands on the phase that
reached the deprecated call — a fixture's setup errors, a test body's fails — with that call chain
in the traceback. Recording the warning and failing the test afterwards would put the failure on
velox's own frame and leave the reader to work out which of a hundred calls produced it.

**CPython's per-module warning registries are bypassed, and dedup is per test.** Those registries
are what make Python report a warning once per source location for the whole process; under a
concurrent runner that means the first test to reach a deprecated call is the only one the summary
can attribute it to. `always` skips them, and each test's collector counts repeats for itself, so
"which tests reach this call" has a real answer. The cost is that `once`, `default` and `module`
collapse to "record it once and stop counting" within a test rather than reproducing three
distinct registry scopes — a distinction that changes a number in the summary and nothing else.

**A warning's location is a filename; a filter's `module` field is a dotted name.** `showwarning`
is handed no module, so the shim resolves the filename back through a `sys.modules` index, falling
back to the filename with `.py` stripped — CPython's own fallback for code no imported module
claims, which is what a test module velox imported by path is. The index is rebuilt only when
`sys.modules` changes size, and only for a run that actually has a `module`-narrowed filter.
