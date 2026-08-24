# Reference

This is a reference of velox public interface, based directly on docstrings.

Everything not exported via `velox/__init__.py` is private and may
move or change without deprecation warning.

- **[Fixtures](fixtures.md)** — `fixture`, `Depends`, `use`.
- **[Marks](marks.md)** — `skip`, `skipif`, `xfail`, `tag`, `timeout`, `solo`, `isolated`, `parametrize`, and the `Skipped`/`Failed` signals.
- **[Built-in fixtures](builtins.md)** — `tmp_path`, `tmp_path_factory`, `capture`, `log_records`, `test_info`.
- **[Assertions](assertions.md)** — `raises`, `approx`, `ExceptionInfo`, `Approx`.
- **[FastAPI](fastapi.md)** — `velox.fastapi` module with  dependency-override helpers for testing a FastAPI app.
- **[Command line](cli.md)** — all the flags `velox` CLI accepts.
