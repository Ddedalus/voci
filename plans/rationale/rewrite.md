# `_assertions/rewrite.py` — assertion introspection

See [rationale.md](../rationale.md) for the index.

**A fallback to `plain` is recorded as data, not just warned about.** When the pyc cache probe
fails, `plan` prints to stderr *and* records the reason on `AssertionSetup`, which the report
header then surfaces. A stderr warning is easy to miss in CI, and a benchmark run that silently
degraded to `plain` measures the wrong thing with nothing in the numbers to say so.
