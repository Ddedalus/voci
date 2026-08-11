"""pytest's assertion rewriter and explanation engine, vendored, not depended on: velox ships no
plugin ecosystem and takes no runtime dependency on pytest.

This is upstream code plus `_shim.py`, deliberately *not* a port of `_pytest/assertion/__init__.py`
— that file is pytest's plugin glue (hook registration, config wiring, per-item save/restore of the
module globals), all of which velox replaces with `velox/_assertions/rewrite.py` and
`velox/_assertions/state.py`.

Import the pieces directly (`from velox._assertions._vendor import rewrite, util`); nothing is
re-exported here, so this package's import cost stays at zero until something is used. See
`VENDOR.md` for what came from where, and `scripts/vendor_assertion.py` for the exact edits —
re-vendoring is a deliberate act, not a tracked upstream.
"""
