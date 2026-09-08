# Marks

A mark is a decorator attaching a fixed value to the underlying function: fixture or test.

For clarity, `skip`, `xfail` or `timeout` applied twice will raise `TypeError`. Othr marks can be stacked freely.

## Skipping and expected failures

::: voci.skip

::: voci.skipif

::: voci.xfail

## Selection and execution

You can assign string labels to tests, which can then be selected via the CLI.

::: voci.tag

## Execution isolation

For tests that may not play nicely with concurrency, voci provides isolated execution marks.

::: voci.timeout

::: voci.solo

::: voci.isolated

## Warnings

::: voci.filterwarnings

See [Warnings](warnings.md) for the filter grammar and how the three tiers of filters are layered.

## Parametrization

::: voci.parametrize

::: voci.case

## Stopping a test from within

Two exception classes are provided to force a test result from within the body.

::: voci.Skipped

::: voci.Failed
