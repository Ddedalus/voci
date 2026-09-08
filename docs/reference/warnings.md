# Warnings

Every warning a run raises is collected, attributed to the test that raised it, and listed at the
end of the run:

```console
--- warnings summary (4) ---
src/myapp/legacy.py:12 DeprecationWarning: old_api() is deprecated, use new_api()
  tests/test_api.py::test_list, tests/test_api.py::test_create (x2)
src/myapp/net.py:88 ResourceWarning: unclosed <socket.socket ...>
  tests/test_net.py::test_fetch
```

Warnings are grouped by what was warned about and where, not listed per test: one deprecated call
reached from a hundred tests is one thing to fix, and the location is what says which. A test that
raised the same warning several times carries the count. A warning raised while no test was running
— during collection, say, or in a session fixture's teardown — is listed under `(no test running)`.

## Filters

A filter decides what happens to a warning: whether it is silenced, reported, or raised as an
exception that fails the test. Filters are written as `action:message:category:module:lineno`, with
every field after the action optional:

| Field | Meaning |
| --- | --- |
| `action` | `default`, `always`, `ignore`, `module`, `once` or `error`, or any unambiguous prefix. |
| `message` | A regex matched, case-insensitively, against the start of the warning's text. |
| `category` | A warning class: a builtin one by name, anything else by its dotted import path. |
| `module` | A regex matched against the start of the dotted name of the module that warned. |
| `lineno` | The line the warning was raised at; `0` matches any. |

`error` raises the warning at the `warnings.warn(...)` that produced it, so the test — or the
fixture — that reached that call fails. `ignore` drops the warning, and it appears in no summary.
The remaining actions all report the warning; `always` counts every occurrence, while `default`,
`module` and `once` record it once and stop counting.

Warnings no filter matches are reported.

## Where filters come from

```toml
[tool.voci]
filterwarnings = [
    "error::DeprecationWarning",
    "ignore:pkg_resources is deprecated:UserWarning",
]
```

```console
$ voci -W error::DeprecationWarning -W ignore::ResourceWarning
```

```python
@voci.filterwarnings("error::DeprecationWarning")
async def test_no_deprecated_calls(): ...
```

The last filter to match a warning is the one that decides it. The three tiers are laid out in that
order — `[tool.voci] filterwarnings` first, then every `-W`, then the marks on the test that raised
the warning — so a mark overrides a `-W`, which overrides the config file, and within one tier a
later entry overrides an earlier one. Stacked `@voci.filterwarnings` decorators follow the same
rule, with the outermost winning.

A mark's filters govern only the test that carries them, whatever else is running at the same time:

```python
@voci.filterwarnings("ignore::DeprecationWarning")
async def test_calls_the_old_api():
    """This test's `ignore` reaches this test alone -- a sibling dispatched alongside it still
    reports the same warning."""
    old_api()
```

::: voci.filterwarnings

## Asserting that something warns

`warnings.catch_warnings()` replaces the process-wide `warnings.filters` list and
`warnings.showwarning` for as long as it is open, so a test that enters it changes what every test
running alongside it sees. Reach for a filter instead where one will do, and keep any
`catch_warnings()` block a test does need under [`@voci.solo`](marks.md), which holds the whole
suite while that test runs.
