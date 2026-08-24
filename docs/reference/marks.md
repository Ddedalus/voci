# Marks

A mark is a decorator that folds one frozen record into the decorated function, where the
collector reads it back in a single attribute lookup. Each decorator hands the same function
object back, so marks stack freely and — apart from `parametrize`, whose stacking order fixes
which argument varies slowest — order does not matter; applying `skip`, `xfail` or `timeout`
twice to one function raises `TypeError` rather than quietly keeping the last one.

`Skipped` and `Failed` are the imperative counterparts of the skip and fail outcomes, raised from
inside a running test or fixture rather than decided ahead of it.

## Skipping and expected failures

::: velox.skip

::: velox.skipif

::: velox.xfail

## Selection and execution

`tag` labels a test for selection; the remaining three shape how the runner schedules and bounds
it.

::: velox.tag

::: velox.timeout

::: velox.solo

::: velox.isolated

## Parametrization

::: velox.parametrize

::: velox.case

## Runtime signals

::: velox.Skipped

::: velox.Failed
