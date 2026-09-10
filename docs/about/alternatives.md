# Alternatives

voci does not aim to be compatible with pytest. There is no plugin ecosystem and no hook system —
dependency injection is the extension point instead. Fixtures are wired through parameter
defaults, not through `conftest.py` and name-based lookup: a fixture is a function you import, so
a typo is an `ImportError` at collection rather than a mystery at run time.

Adopting voci means rewriting your fixture wiring. It also requires Python 3.13+. Linux and macOS
are supported; Windows is best-effort.

## `unittest.mock`

`unittest.mock` keeps working, at a price. `mock.patch` writes to a module or class, which every
concurrently running test would see, so voci runs a patch-decorated test alone and reports what
that cost. A patch opened inside a test body fails that test unless it is marked `@voci.solo`.

`examples/02-async-library` walks the alternatives.
