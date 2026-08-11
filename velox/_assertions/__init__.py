"""Assertion introspection: `raises`/`approx`, and the machinery behind rewritten `assert`s.

`raises.py` and `approx.py` are the two assertion helpers velox provides directly. `state.py`
holds the per-test `ContextVar`s the vendored explanation engine reads. `rewrite.py` installs the
assertion-rewrite import hook and glues to the vendored engine in `_vendor/`. `pep657.py` handles
the fallback path: caret-span underlining for an `assert` the rewriter never touched.
"""
