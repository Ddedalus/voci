# Reference

The public API, one page per group of symbols, rendered from the docstrings in the source — so a
signature here is the signature you will import.

`velox/__init__.py` is the whole public surface; everything else in the package is private and may
move.

- **[Fixtures](fixtures.md)** — `fixture`, `Depends`, `use`, and the types they involve
  (`Fixture`, `Injection`, `Scope`).
- **[Marks](marks.md)** — `skip`, `skipif`, `xfail`, `tag`, `timeout`, `solo`, `isolated`,
  `parametrize`, the records they attach, and the `Skipped`/`Failed` signals that raise the same
  outcomes from inside a test.
- **[Built-in fixtures](builtins.md)** — `tmp_path`, `tmp_path_factory`, `capture`, `log_records`,
  `test_info`, and their result types.
- **[Assertions](assertions.md)** — `raises`, `approx`, `ExceptionInfo`, `Approx`.
- **[FastAPI](fastapi.md)** — `velox.fastapi`, a separate module imported by name, holding the
  per-test dependency-override helpers for testing a FastAPI app.
- **[Command line](cli.md)** — every flag `velox` accepts, and the exit code each run ends on.
