# Rationale

Why velox is shaped the way it is - back story on decisions made. It covers important choices only, where no natural documentation exists in code or docs.

- [rationale/global.md](rationale/global.md) — decisions that apply across the whole system
- [rationale/collection.md](rationale/collection.md) — `_collection/collect.py`, import and collection
- [rationale/di.md](rationale/di.md) — `_di/fixtures.py` / `_di/runtime.py`, dependency injection
- [rationale/fastapi.md](rationale/fastapi.md) — `fastapi.py`, per-test dependency overrides
- [rationale/builtins-fixtures.md](rationale/builtins-fixtures.md) — `_builtins/fixtures.py`, built-in fixtures
- [rationale/run.md](rationale/run.md) — `_run/run.py`, execution
- [rationale/safety.md](rationale/safety.md) — `_run/safety.py`, the loop watchdog and tests that check nothing
- [rationale/warnings.md](rationale/warnings.md) — `_warnings.py`, warning filters
- [rationale/mocking.md](rationale/mocking.md) — `_mocking.py`, `unittest.mock` patching
- [rationale/isolated.md](rationale/isolated.md) — `_run/isolated.py`, the `@velox.isolated` subprocess tier
- [rationale/coverage.md](rationale/coverage.md) — `_run/coverage.py`, coverage across the isolated boundary
- [rationale/capture.md](rationale/capture.md) — `_builtins/capture.py`, capture and routing
- [rationale/rewrite.md](rationale/rewrite.md) — `_assertions/rewrite.py`, assertion introspection
- [rationale/cli.md](rationale/cli.md) — `cli.py`, entrypoint
- [rationale/terminal.md](rationale/terminal.md) — `_report/terminal.py`, reporting
