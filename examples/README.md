# voci examples

Three self-contained example suites — application code plus tests — showing voci's syntax and
scheduling model in realistic settings.

| Example | App under test | Demonstrates |
|---|---|---|
| [01-fastapi-crud](01-fastapi-crud/) | FastAPI + async SQLAlchemy CRUD API | one shared `app` instance, transaction-rollback test isolation, `voci.fastapi.client` |
| [02-async-library](02-async-library/) | webhook delivery client, stdlib only | dependency injection as the default alternative to `unittest.mock.patch` |
| [03-shared-resources](03-shared-resources/) | ledger over `sqlite3` + a TCP receiver | `exclusive=` resource tokens, `@voci.solo`, data isolation as the default |

Each directory is self-contained: application code, test suite, `pyproject.toml`, and a README with
the commands to run it and what to look at in the source.

## Running them

```bash
cd examples/01-fastapi-crud
uv sync
voci
```

`02-async-library` and `03-shared-resources` have no application dependencies beyond voci itself, so
there's no lock file to sync:

```bash
cd examples/02-async-library
uv venv && uv pip install -e ../..
voci
```

Each test injects via `Annotated[T, Depends(fixture)]` metadata, not a default, so none of these
suites need ruff's `B008` bugbear exemption for `voci.Depends` — only 01's FastAPI application
code, which still takes `fastapi.Depends()` as a default the way FastAPI itself does.
