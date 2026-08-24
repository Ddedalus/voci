# Marks

A mark is a decorator attaching a fixed value to the underlying function: fixture or test.

For clarity, `skip`, `xfail` or `timeout` applied twice will raise `TypeError`. Othr marks can be stacked freely.

## Skipping and expected failures

::: velox.skip

::: velox.skipif

::: velox.xfail

## Selection and execution

You can assign string labels to tests, which can then be selected via the CLI.

::: velox.tag

## Execution isolation

For tests that may not play nicely with concurrency, velox provides isolated execution marks.

::: velox.timeout

::: velox.solo

::: velox.isolated

## Parametrization

::: velox.parametrize

::: velox.case

## Stopping a test from within

Two exception classes are provided to force a test result from within the body.

::: velox.Skipped

::: velox.Failed
