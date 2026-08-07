"""pytest's assertion rewriter and explanation engine, vendored.

This package is upstream code plus `_shim.py`. It is deliberately *not* a port of
`_pytest/assertion/__init__.py` — that file is pytest's plugin glue (hook registration, config
wiring, per-item save/restore of the module globals), all of which velox replaces with
`velox/_rewrite.py` and `velox/_assertion_state.py`.

Import the pieces directly (`from velox._vendor.assertion import rewrite, util`); nothing is
re-exported here, so this package's import cost stays at zero until something is used.
"""
