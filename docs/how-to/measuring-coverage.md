# Measuring coverage

Coverage is measured by running [coverage.py](https://coverage.readthedocs.io/) around velox —
no plugin to install, no velox flag to pass:

```console
$ coverage run -m velox
$ coverage report -m
Name                    Stmts   Miss  Cover   Missing
-----------------------------------------------------
ledger/__init__.py          2      0   100%
ledger/store.py            84      3    96%   61, 118-119
-----------------------------------------------------
TOTAL                      86      3    97%
```

`coverage html` and `coverage xml` read the same data file. Everything a project already has in
`[tool.coverage.run]` — `source`, `branch`, `omit`, exclusions, plugins — applies unchanged:
coverage.py is reading its own configuration, not velox's.

Use the module form, `coverage run -m velox`, rather than `coverage run velox`. It finds velox
without the console script being on `PATH`, which containers and `--user` installs are not always
arranged to guarantee.

## Isolated tests

A velox suite is one process running many tests at once, so a single `coverage run` measures all
of them: the event loop's thread, and the worker threads sync `def` tests run on.

`@velox.isolated` tests each run in a subprocess instead. velox puts those subprocesses under the
same measurement as the run that spawned them, with the settings the parent is measuring under,
and merges back what each one recorded as it exits — so a run produces one data file to report on,
whatever mix of tests it contained.

```python
@velox.isolated
async def test_install_rebinds_the_signal_handler() -> None:
    """Runs in its own subprocess, and is measured there: every line `install` executes
    counts towards the report the same way an in-process test's would."""
    reloader.install()

    assert signal.getsignal(signal.SIGHUP) is not signal.SIG_DFL
```

Carrying a configuration into a subprocess needs coverage.py 7.10 or newer. Below that, velox says
so on stderr, once per isolated test whose lines are missing from the report.

## In CI

```yaml
- run: coverage run -m velox
- run: coverage report --fail-under=90
```

Keeping `--fail-under` on the report step keeps two different failures apart: a suite that fails
and a suite that covers too little are separate problems, and one exit code for both hides the
first behind the second.
