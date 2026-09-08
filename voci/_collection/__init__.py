"""Collection: finding candidate test files and turning them into `TestRecord`s.

`discovery.py` walks the filesystem for files matching the test patterns. `collect.py` imports
each one under a synthetic module name and builds the flat list of `TestRecord`s the runner
executes, resolving each test's dependency-injection plan via `voci._di.fixtures`. `requires.py`
holds `voci.use(...)`, by which a module declares fixtures for every test it defines.
"""
