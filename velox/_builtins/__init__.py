"""Built-in fixtures and the capture/routing runtime that backs them.

`fixtures.py` declares `tmp_path`, `tmp_path_factory`, `tmpdir`, `tmpdir_factory`, `capture`,
`log_records` and `test_info` as `Fixture` objects, and the result types they hand back.
`capture.py` routes captured stdout/stderr, logging, and `tmp_path` allocation for whichever test
is currently running, through the providers `fixtures.py` wires in. The two modules import each
other -- see plans/rationale/builtins-fixtures.md for why that's deliberate and safe.
"""
