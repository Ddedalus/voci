# Selecting tests

`voci` with no arguments runs everything under the configured `testpaths`. Everything else here
narrows that down to a file, a single test, or a subset picked by expression.

## By path or id

A path argument can name a file, a directory, or one test inside a file with `::`:

```console
$ voci tests/test_users.py
$ voci tests/test_users.py::test_create_user
```

A parametrized test's cases each get their own id, `test_name[case]` — run one case the same way:

```console
$ voci tests/test_users.py::test_create_user[admin]
```

Several paths on one command line run together, in the order collection finds them.

## `-k`: selecting by substring

`-k EXPR` runs only tests whose id matches a boolean expression, e.g. `-k "users and not slow"`.
Each term is matched as a case-insensitive substring against the whole id — file path, test name,
and `[case]` suffix all count — so `-k users` selects every test in `tests/test_users.py` as well
as any test elsewhere with "users" in its name.

```console
$ voci -k "users and not slow"
```

A term with a dash, a dot, or brackets isn't a bare identifier and needs quoting for the shell and
the expression parser both:

```console
$ voci -k "'test_create[admin]'"
```

## `-m`: selecting by tag

`-m EXPR` runs the same kind of boolean expression, over `@voci.tag(...)` names instead of the id:

```console
$ voci -m "smoke"
$ voci -m "smoke and not flaky"
```

Tags themselves, and how they stack on a test, are covered in [Tags and
selection](marks.md#tags-and-selection) — this is the flag that reads them.

## Deselected, not skipped

A test that `-k` or `-m` filters out is deselected: it never runs, and it never shows up as
`SKIPPED` in the report. `@voci.skip` is the only thing that produces a `SKIPPED` result, and a
skip-marked test is skipped regardless of what `-m` says about its tags.

## Narrowing a run: `--serial` and `-x`

`--serial` runs one test at a time in collection order, and `-x` (or `--maxfail N`) stops the run
after the first failure (or after N). Neither changes which tests are selected; both shrink how
much of the run you have to read to find the one that matters. [Debugging a flaky
test](../how-to/debugging-a-flaky-test.md) covers both with a worked example:

```console
$ voci --serial -x tests/test_users.py
```

## Checking a selection before trusting it

`--collect-only` prints the id of every test the current selection would run, without running any
of them — the way to check that a `-k` expression against a suite you don't know well selects what
you think it does, before spending a run's wall time on it:

```console
$ voci -k "users and not slow" --collect-only
```

[Command line](../reference/cli.md) has the full reference for every flag here, plus the rest of
`voci`'s options.
