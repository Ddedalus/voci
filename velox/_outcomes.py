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
    `@velox.skip`/`@velox.skipif`, which decide once at collection instead. `_run.run._run_one`
    catches this in both the setup and call phase and reports `Outcome.SKIPPED`, with
    `str(self)` as the reason; whatever ran before the raise already ran, and fixtures already
    acquired are still torn down normally.

    A `BaseException`, not an `Exception` -- like pytest's own `Skipped`, so a test or fixture
    body's `except Exception:` doesn't accidentally swallow the skip signal it was never meant to
    catch.
    """


class Failed(BaseException):
    """Raise to fail the running test immediately, with a message rather than an assertion --
    the runtime counterpart of writing `assert False, msg`. Caught nowhere specially in
    `_run.run`: any exception already fails a test's call phase, so this is just a named
    spelling of "fail with this message", for the cases pytest's own `pytest.fail()` covers.

    A `BaseException`, not an `Exception`, for the same reason `Skipped` is: a broad
    `except Exception:` around a test's own code must not swallow a deliberate failure signal.
    """
