# Configuration

Everything in this page is optional — `voci` with no `pyproject.toml` in sight runs against
`tests/`, or the current directory if there's no `tests/`, at the built-in defaults.

## Where `[tool.voci]` lives

voci reads one table, `[tool.voci]`, from one `pyproject.toml`. Starting from the paths you gave
on the command line (or the current directory, if you gave none), it walks upward looking for a
`pyproject.toml` that declares `[tool.voci]`, stopping once it reaches a directory containing
`.git`. The directory holding that file becomes the run's rootdir — relative paths in the config
(`testpaths`, `ignore`) resolve against it, not against your shell's working directory. There's no
inheritance: one project, one table, however deep in a monorepo you ran `voci` from.

```toml
[tool.voci]
testpaths = ["tests"]
concurrency = 16
timeout = 60
env = { ENVIRONMENT = "test" }
```

An unrecognized key, or a value of the wrong type — `concurrency = "16"`, say — is a startup
error naming the file and the key, not a value voci quietly ignores.

## Precedence

A flag typed on the command line beats the config file, which beats the built-in default:

```bash
voci --concurrency 4          # wins, regardless of what pyproject.toml says
```

```toml
[tool.voci]
concurrency = 16               # used when no --concurrency is given
```

Leave a setting out of both and voci falls back to its own default — 16 for concurrency, no limit
for `timeout`, and so on for the rest of the keys below. [Concurrency](concurrency.md) covers
`concurrency` and `timeout` in full, including this same precedence rule applied to each.

## `testpaths`

Where voci collects from when you run `voci` with no paths on the command line:

```toml
[tool.voci]
testpaths = ["tests", "integration"]
```

Paths on the command line — `voci tests/test_users.py` — always win over `testpaths`; it only
supplies the default when you give none.

## `test_file_patterns`

Which filenames, under the collected roots, voci treats as test files. The default is
`["test_*.py", "*_test.py"]`; setting this replaces that list rather than adding to it.

## `ignore`

Directory names or path suffixes excluded from collection. voci skips some directories on its own
(`.git`, `.venv`, `__pycache__`, `node_modules`, and the like); setting `ignore` in config replaces
that built-in list rather than adding to it, so include the defaults you want kept alongside
whatever you're adding.

## `env`

Environment variables set for the run, before any test or fixture executes:

```toml
[tool.voci]
env = { ENVIRONMENT = "test", DATABASE_URL = "sqlite+aiosqlite:///./test.db" }
```

Each variable is restored to whatever it was (or unset, if it wasn't set at all) once the run
finishes.

## `loop_watchdog`

The config-file form of `--loop-watchdog`: how many seconds the event loop may go unresponsive
before voci names the call holding it. `0` switches the diagnostic off.

```toml
[tool.voci]
loop_watchdog = 10
```

[Debugging a flaky test](../how-to/debugging-a-flaky-test.md) covers reading the warning this
produces.

## `filterwarnings`

`[tool.voci] filterwarnings` sets the suite-wide warning filters, the lowest-precedence of the
three tiers `-W` and `@voci.filterwarnings` also contribute to — see
[Warnings](../reference/warnings.md) for the full picture.

## The rest

[Command line](../reference/cli.md) lists every flag alongside its `[tool.voci]` key, for the
exhaustive version of the table above.
