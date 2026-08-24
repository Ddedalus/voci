# Assertions

velox vendors pytest's assertion rewriter, so a plain `assert` reports a real diff of the two
sides. Two things a plain `assert` cannot express get a helper each: asserting that a block
raises, and comparing floats with a tolerance. The block `raises` opens binds an `ExceptionInfo`
holding the exception it caught, so the block can go on to assert about it; `approx` hands back
an `Approx` that compares equal to anything within tolerance, from either side of the `==`.

## Raising

::: velox.raises

::: velox.ExceptionInfo

## Tolerant comparison

::: velox.approx

::: velox.Approx
