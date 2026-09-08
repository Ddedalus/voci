# Remedy per code

`.matrix[code].action` in `findings.json` is authoritative; this restates it with the seam named
and the voci spelling filled in. Take the seam unless the gate rule in SKILL.md sends you to the
mark.

## Serialized: counted by `.totals.serialized_percent`

| Code | Site | Seam | Mark |
|---|---|---|---|
| `VC401` | `monkeypatch.<call>` — `.message` names it | `setattr`/`setitem`: pass the collaborator as an injected fixture parameter instead of rebinding it. `setenv`/`delenv`: `[tool.voci] env` when the value is suite-wide, a config-object fixture when it is per-test. `syspath_prepend`: fix the package layout. | `@voci.solo`, **except** `monkeypatch.chdir` → `@voci.isolated` |
| `VC402` | `os.environ[...] = `, `.update`, `.pop`, `os.putenv`, `os.unsetenv` | Suite-wide constant → `[tool.voci] env`. Per-test value → read it through an injected settings object rather than `os.environ`. | `@voci.solo` |
| `VC403` | `warnings.simplefilter`, `filterwarnings`, `resetwarnings`, `catch_warnings` | Assert on what the warning accompanies (the return value, the log record) rather than on the warning. | `@voci.solo` |
| `VC404` | `logging.basicConfig`, `logging.disable`, `.setLevel`, `.addHandler`, `.removeHandler`, `logging.root.*` | `voci.log_records` captures without touching levels. | `@voci.solo` |
| `VC405` | `sys.modules[...] = `, `sys.modules.pop`, `del sys.modules[...]`, `importlib.reload` | Restructure so the module need not be reloaded — usually a module-level constant that should be read at call time. | `@voci.isolated` (required; solo does not help, the rebinding outlives the test) |
| `VC406` | `os.chdir`, `contextlib.chdir` | Absolute paths, rooted at `voci.tmp_path`. | `@voci.isolated` |
| `VC407` | `locale.setlocale`, `decimal.setcontext`, `decimal.localcontext`, `sys.setrecursionlimit`, `sys.setswitchinterval` | Pass the context to the code under test where its API allows it. | `@voci.isolated` |
| `VC408` | `freezegun.freeze_time`, `time_machine.travel` | Inject a clock: the code under test takes a `now()` callable, the fixture supplies a fake. | `@voci.solo` |
| `VC322` | a plugin that mutates process-global state | Drop the plugin's use. | `@voci.solo`, or `@voci.isolated` if what it mutates is interpreter-level |
| `VC205` | `caplog.set_level(...)` | Narrow the region that needs the level, then as `VC404`. | `@voci.solo` |
| `VC216` | `pytest.warns`, `recwarn`, `pytest.deprecated_call` (unsupported) | Assert on what the warning accompanies. | catch inside a `@voci.solo` test |
| `VC219` | `mocker` (pytest-mock) | Rewrite each `mocker.` call as the `mock` call it wraps; the result is then `VC217` or `VC218`. | — |
| `VC217` | `mock.patch` as a decorator | **Nothing to do.** voci reads the patching off the function object at collection and schedules the test alone by itself. | — |
| `VC218` | `mock.patch` as a context manager | **Nothing to do.** `convert` already wrote `@voci.solo` on the tests the audit charged the site to. | — |

`VC217`/`VC218` still count toward `serialized_percent`, and the only way to move them is to stop
patching — inject the collaborator instead. A `GlobalPatchError` at run time means a
context-manager patch reached a test `convert` did not mark; add `@voci.solo` there.

## Not serialized: correctness under concurrency

Marks do not answer these. `@voci.solo` masks `VC411` and `VC412` at full serial cost and is the
wrong trade; it does not help `VC409` at all, which fails under `--serial` too.

| Code | Site | Fix |
|---|---|---|
| `VC409` | `asyncio.run`, `get_event_loop`, `new_event_loop`, `set_event_loop`, `run_until_complete`, `run_forever` | voci already runs the test on a running loop. Make the test `async def` and `await` the coroutine directly. |
| `VC410` | a blocking call inside an `async def` body | Use the async client, or make the test a plain `def` — a sync test runs on an executor thread and holds only its own slot. |
| `VC411` | `global x`, or a write to a module/class-level name | Move the state into a fixture. |
| `VC412` | `random.seed`, `numpy.random.seed`, `reset_sequence`, Faker seeding | Seed per test, or assert on shape rather than on generated values. |
| `VC413` | a hardcoded port, host:port, or filesystem path | Allocate per test. Where the resource is genuinely singular, put `exclusive=` on the fixture that owns it: `@voci.fixture(exclusive="postgres")`. Only the tests reaching that fixture serialize against each other. |

## solo vs isolated

- `@voci.solo` — admitted only once nothing else is running, and blocks every other admission
  until it finishes. Same process, same interpreter, same loop. Sufficient for state that is
  restored before the test returns (a patched attribute, an env var a fixture unsets, a warning
  filter under `catch_warnings`).
- `@voci.isolated` — a fresh subprocess with its own interpreter and loop, admitted through the
  same gate. Required where the state is interpreter-level or survives the test: module identity,
  working directory, locale, decimal context, recursion limit. A module-scope fixture the test
  shares with in-process siblings is set up and torn down separately inside the subprocess.
