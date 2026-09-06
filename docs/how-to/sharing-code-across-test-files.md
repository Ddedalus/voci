# Sharing code across test files

velox handles two kinds of imports differently: the suite's own code, and the test files it
collects. This page covers both, and how they interact when you split a suite across directories.

## The rootdir

Every run picks a rootdir first. The search starts at the common ancestor of the paths given on
the command line, or the current directory if none were given. It walks upward from there, looking
for a `pyproject.toml` with a `[tool.velox]` table. A `pyproject.toml` without that table doesn't
stop the walk — a package nested inside a bigger repo, say. The walk stops at the first directory
holding a `.git`, checking that directory's own `pyproject.toml` first but never looking past it.
If it reaches the filesystem root with no match and no `.git`, it stops there too.

When nothing is found, the rootdir is wherever the search started, and there's no config. The
startup header prints `config: none` in that case.

Relative config — `testpaths`, `ignore` — is resolved against the rootdir. Test ids are shown
relative to it. `.velox_cache/lastfailed.json` lives under it. And it's the one directory velox
puts on `sys.path`.

## rootdir on sys.path

Before collecting the first file, velox inserts the rootdir at `sys.path[0]`, and removes it again
once the run ends. It goes at the front, not the back, so the suite's own sources shadow an
installed package of the same name.

No `__init__.py` is required anywhere for this to work: PEP 420 namespace packages let any
directory under the rootdir be imported by its dotted path, as long as the rootdir is on
`sys.path`. So this resolves:

```python
# tests/test_delivery.py
from tests.fixtures import settings
```

`tests.fixtures` is an ordinary import. It's cached in `sys.modules`, it can be imported from more
than one place, and it can use relative imports of its own. velox does nothing special to it.

## Test files are imported differently

Collection doesn't use `sys.path` to reach the files it walks for tests. Each one is imported
straight from its file path, under a synthetic name shaped like `velox_tests.<escaped relative
path>`. Once collection is done with a file, its module is dropped from `sys.modules` again.

The synthetic name keeps two files both named `test_utils.py`, in different directories, from
colliding under one module name. pytest calls the collision this avoids `ImportPathMismatchError`.

Two things follow from this. A relative import inside a test file has nothing to resolve against —
`from .fixtures import settings` fails, naming a `velox_tests` package that doesn't exist. And one
test file can't import another by name, because there's no real module behind it to reach. Code
shared between test files needs to live in an ordinary module instead, imported the way
`tests.fixtures` was above.

## Packages above a test file

A package's `__init__.py` is imported the same way, ahead of the test file itself, so a
`velox.use(...)` call in it reaches every test below. This walk stops at the first directory with
no `__init__.py`. It has nothing to do with the rootdir search above.

## Isolated tests

A `@velox.isolated` test runs in its own subprocess. That subprocess repeats the rootdir-on-
`sys.path` step before importing anything, so its imports behave the same as the parent run's.

## A layout that works

```
pyproject.toml       # [tool.velox] lives here -- this directory is the rootdir
tests/
  fixtures.py         # not a test file -- shared by whatever imports it
  test_users.py
  test_orders.py
  api/
    __init__.py       # optional, for a velox.use(...) that covers this directory
    fixtures.py
    test_api.py
```

```python
# tests/test_users.py
from tests.fixtures import settings

# tests/api/test_api.py
from tests.api.fixtures import payload
```

Both are ordinary absolute imports. They resolve because `tests/` sits under the rootdir on
`sys.path`, not because either file is a collected test module.
