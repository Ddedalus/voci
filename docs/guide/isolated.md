# Isolated tests

`@voci.solo` and `@voci.isolated` are for a test that can't safely run next to any other test at
all. Both are applied bare, with no parentheses.

## `@voci.solo`

```python
@voci.solo
def test_feature_flag_disabled_hides_the_beta_route() -> None:
    previous = config.FEATURE_X_ENABLED
    config.FEATURE_X_ENABLED = False
    try:
        assert not config.feature_x_available()
    finally:
        config.FEATURE_X_ENABLED = previous
```

A `@voci.solo` test is admitted only once nothing else is running, and blocks every other test's
admission until it finishes. It still runs in the same process as the rest of the suite — just
never concurrently with another test.

Reach for `solo` when a test mutates state with no owner narrower than the whole process — an
environment variable, a module attribute patched by hand outside a fixture. None of these fits
`exclusive=`'s token model: the mutation isn't scoped to a resource, it's global to the process.
`solo` only protects the mutation from *concurrent* tests, though — restoring it, as
`FEATURE_X_ENABLED` does above, is still the test's own job.

## `@voci.isolated`

```python
@voci.isolated
def test_relative_config_path_resolves_from_cwd() -> None:
    os.chdir("/etc/myapp")
    assert Path("myapp.toml").exists()
```

`os.chdir` has no per-task equivalent in CPython — one working directory for the whole process —
so a test that changes it can't just restore it under `solo` the way `FEATURE_X_ENABLED` does
above: a crash or a timeout between the `chdir` and its restore would leave every other test in
the run working from the wrong directory, for the rest of the run. `@voci.isolated` runs the test
alone in a subprocess, on a fresh interpreter and event loop, so nothing it does to that process's
global state outlives it.

The parent process hands the subprocess the test's id, its own rootdir, and its assertion-rewrite
decision; the subprocess re-collects and runs that one test the way the parent would have, and
reports the outcome, duration, captured output, log records, and warnings back as JSON. voci turns
that into an ordinary `TestResult` — an isolated test's entry in the report looks like any other
test's.

The subprocess is what's fresh; admission works exactly like any other test's. The same
`--concurrency` cap, the same `exclusive=` bookkeeping, and the same solo-lock check all apply, so
an isolated test doesn't start early and doesn't get a concurrency slot of its own outside
`--concurrency`. If it shares a `scope="module"` (or wider) fixture with other tests in the same
file, the subprocess builds and tears down its own instance separately from theirs.

Reach for `isolated` when a subprocess boundary is the only real fix: a `chdir` or other
process-global state a crash could leave behind, a C extension that can crash the interpreter, a
test that needs to exercise a real process exit or signal. It costs a process spawn per test, so
try `solo` first if restoring the state by hand, in-process, is enough.

A test that patches with `unittest.mock` as a decorator is scheduled solo automatically, without
`@voci.solo` written on it — [Mocking](mocking.md) covers why. [Marks](../reference/marks.md) has
the full reference entry for both marks.
