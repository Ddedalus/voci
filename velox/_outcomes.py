"""`Skipped`/`Failed`: the runtime counterparts of pytest's imperative `pytest.skip()`/
`pytest.fail()`, raised directly rather than read off a mark.

A leaf module -- no imports of its own -- so both `_di.runtime` and `_run.run` can import it
without either importing the other. `_di.runtime`'s fixture-teardown batching needs the two
types by name to fold a `Skipped`/`Failed` raised from a fixture's post-`yield` code into an
ordinary teardown failure rather than -- since both are `BaseException`s, not `Exception`s --
treat it like the `KeyboardInterrupt`/`SystemExit`/`CancelledError` its `except Exception` is
deliberately narrowed to let through unfolded. `_run.run` is where the two actually mean
something: `_run_one` catches either during setup or the call phase and reports
`Outcome.SKIPPED`.
"""

from __future__ import annotations

__all__ = ["Failed", "Skipped"]


class Skipped(BaseException):
    """Raise to skip the running test immediately -- the runtime counterpart of
    `@velox.skip`/`@velox.skipif`, which decide once at collection instead.

    Raised from a fixture or from the test body, it reports the test as skipped with `str(self)`
    as the reason; whatever ran before the raise already ran, and fixtures already acquired are
    torn down normally. A `BaseException`, not an `Exception`, so a test or fixture body's
    `except Exception:` does not swallow the skip signal.
    """


class Failed(BaseException):
    """Raise to fail the running test immediately, with a message rather than an assertion --
    a named spelling of `assert False, msg`.

    A `BaseException`, not an `Exception`, for the same reason `Skipped` is: a broad
    `except Exception:` around a test's own code must not swallow a deliberate failure signal.
    """
