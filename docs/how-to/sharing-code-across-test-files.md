# Sharing code across test files

voci handles two kinds of imports differently: the suite's own code, and the test files it
collects. This page covers both, and how they interact when you split a suite across directories.

## The rootdir

Every run picks a rootdir first. The search starts at the common ancestor of the paths given on
the command line, or the current directory if none were given. It walks upward from there, looking
for a `pyproject.toml` with a `[tool.voci]` table. A `pyproject.toml` without that table doesn't
stop the walk — a package nested inside a bigger repo, say. The walk stops at the first directory
holding a `.git`, checking that directory's own `pyproject.toml` first but never looking past it.
If it reaches the filesystem root with no match and no `.git`, it stops there too.

If no `[tool.voci]` table was found, the rootdir is the directory of the nearest plain
`pyproject.toml` the walk passed — the startup header's `config: none` line names that case. Only
when there was no `pyproject.toml` at all does the rootdir fall back to wherever the search
started.

Relative config — `testpaths`, `ignore` — is resolved against the rootdir. Test ids are shown
relative to it. `.voci_cache/lastfailed.json` lives under it. And it's the one directory voci
puts on `sys.path`.

Which is why a `pyproject.toml` anchors it, rather than the arguments. Every one of those four
would otherwise change with the arguments: `voci` and `voci tests/` would print ids in different
spellings, read different `.voci_cache` directories, and resolve `from tests.fixtures import ...`
in one case and not the other. Put a `pyproject.toml` at the root of any project you want the same
answer from twice — an empty one is enough, and `[tool.voci]` makes it explicit.

The corollary is that a test file cannot import a module sitting beside it by a bare name. Under a
project root, `tests/helper.py` is `tests.helper`, never `helper`. Reach it by its full dotted path
from the rootdir, exactly as the next section does.

## rootdir on sys.path

Before collecting the first file, voci inserts the rootdir at `sys.path[0]`, and removes it again
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
than one place, and it can use relative imports of its own. voci does nothing special to it.

## Test files are imported differently

Collection doesn't use `sys.path` to reach the files it walks for tests. Each one is imported
straight from its file path, under a synthetic name shaped like `voci_tests.<escaped relative
path>`. Once collection is done with a file, its module is dropped from `sys.modules` again.

The synthetic name keeps two files both named `test_utils.py`, in different directories, from
colliding under one module name. pytest calls the collision this avoids `ImportPathMismatchError`.

Two things follow from this. A relative import inside a test file has nothing to resolve against —
`from .fixtures import settings` fails, naming a `voci_tests` package that doesn't exist. And one
test file can't import another by name, because there's no real module behind it to reach. Code
shared between test files needs to live in an ordinary module instead, imported the way
`tests.fixtures` was above.

## Packages above a test file

A package's `__init__.py` is imported the same way, ahead of the test file itself, so a
`voci.use(...)` call in it reaches every test below. This walk stops at the first directory with
no `__init__.py`. It has nothing to do with the rootdir search above.

## Isolated tests

A `@voci.isolated` test runs in its own subprocess. That subprocess repeats the rootdir-on-
`sys.path` step before importing anything, so its imports behave the same as the parent run's.

## A layout that works

```
pyproject.toml       # [tool.voci] lives here -- this directory is the rootdir
tests/
  fixtures.py         # not a test file -- shared by whatever imports it
  test_users.py
  test_orders.py
  api/
    __init__.py       # optional, for a voci.use(...) that covers this directory
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
