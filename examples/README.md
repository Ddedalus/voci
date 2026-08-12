# velox examples

Three self-contained example suites — application code plus tests — showing velox's syntax and
scheduling model in realistic settings.

| Example | App under test | Demonstrates |
|---|---|---|
| [01-fastapi-crud](01-fastapi-crud/) | FastAPI + async SQLAlchemy CRUD API | one shared `app` instance, transaction-rollback test isolation, `velox.fastapi.client` |
| [02-async-library](02-async-library/) | webhook delivery client, stdlib only | dependency injection as the default alternative to `unittest.mock.patch` |
| [03-shared-resources](03-shared-resources/) | ledger over `sqlite3` + a TCP receiver | `exclusive=` resource tokens, `@velox.solo`, data isolation as the default |

Each directory is self-contained: application code, test suite, `pyproject.toml`, and a README with
the commands to run it and what to look at in the source.

## Running them

```bash
cd examples/01-fastapi-crud
uv sync
velox
```

`02-async-library` and `03-shared-resources` have no application dependencies beyond velox itself, so
there's no lock file to sync:

```bash
cd examples/02-async-library
uv venv && uv pip install -e ../..
velox
```

Every example's `pyproject.toml` sets

```toml
[tool.ruff.lint.flake8-bugbear]
extend-immutable-calls = ["velox.Depends"]
```

Without it, ruff's B008 fires on every test that takes a `Depends(...)` default.
