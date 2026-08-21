# Reference

The public API, one page per group of symbols, rendered from the docstrings in the source — so a
signature here is the signature you will import.

`velox/__init__.py` is the whole public surface; everything else in the package is private and may
move. It exports four groups:

- **Fixtures** — `fixture`, `Depends`, `use`, and the types they involve (`Fixture`, `Injection`,
  `Scope`).
- **Marks** — `skip`, `skipif`, `xfail`, `tag`, `timeout`, `solo`, `isolated`, `parametrize`, and
  the records they attach.
- **Built-in fixtures** — `tmp_path`, `tmp_path_factory`, `capture`, `log_records`, `test_info`,
  and their result types.
- **Assertions** — `raises`, `approx`, `ExceptionInfo`, `Approx`.

`velox.fastapi` is a separate module, imported by name, holding the per-test dependency-override
helpers for testing a FastAPI app.

The command line is documented alongside these, generated from `velox --help` so the flags stay in
step with the runner.
