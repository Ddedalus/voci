# Sharing code across test files

velox splits importing in two: one mechanism handles the suite's own code, a separate one handles
the test files it collects. The split is what makes sharing code across files predictable.

## The rootdir

Every run picks a rootdir before doing anything else. The search starts at the common ancestor of
the paths given on the command line (the current directory, if none were given), then walks upward
looking for a `pyproject.toml` with a `[tool.velox]` table. A `pyproject.toml` without that table
doesn't stop the walk — a package nested inside a bigger repo, say. The walk also stops at the
first directory holding a `.git`, checking that directory's own `pyproject.toml` first but never
looking past it. Reaching the filesystem root with no match and no `.git` stops it too.

If no `[tool.velox]` table was found, the rootdir is the directory of the nearest plain
`pyproject.toml` the walk passed — the startup header's `config: none` line names that case. Only
when there was no `pyproject.toml` at all does the rootdir fall back to wherever the search
started.

The rootdir does double duty: it's what `testpaths` and `ignore` are resolved against, what test
ids are shown relative to, where `.velox_cache/lastfailed.json` lives, and the one directory velox
puts on `sys.path`.

Which is why a `pyproject.toml` anchors it, rather than the arguments. Every one of those four
would otherwise change with the arguments: `velox` and `velox tests/` would print ids in different
spellings, read different `.velox_cache` directories, and resolve `from tests.fixtures import ...`
in one case and not the other. Put a `pyproject.toml` at the root of any project you want the same
answer from twice — an empty one is enough, and `[tool.velox]` makes it explicit.

The corollary is that a test file cannot import a module sitting beside it by a bare name. Under a
project root, `tests/helper.py` is `tests.helper`, never `helper`. Reach it by its full dotted path
from the rootdir, exactly as the next section does.

## rootdir on sys.path

Before collecting the first file, velox inserts the rootdir at `sys.path[0]` and removes it again
once the run ends. Prepended, not appended, so the suite's own sources shadow an installed package
of the same name.

That single insertion is the whole import setup. No `__init__.py` is required anywhere: PEP 420
namespace packages let any directory under the rootdir be imported by its dotted path as long as
the rootdir is on `sys.path`. That's what resolves an ordinary import like this:

```python
# tests/test_delivery.py
from tests.fixtures import settings
```

`tests.fixtures` behaves like any other import — cached in `sys.modules`, safe to import from more
than one place, free to use relative imports of its own. None of that is velox-specific.

## Test files are imported differently

A file collection walks for its own tests doesn't go through `sys.path`. It's imported straight
from its file path, under a synthetic name shaped like `velox_tests.<escaped relative path>`, and
dropped from `sys.modules` again once collection is done with it.

The synthetic name is what keeps two files named `test_utils.py` in different directories from
colliding under one module name — pytest calls this `ImportPathMismatchError`; velox avoids it by
construction.

It costs a test file two things. A relative import has nothing to resolve against: `from .fixtures
import settings` fails, naming a `velox_tests` package that doesn't exist. And one test file can't
import another by name — there's no real module behind it to reach. Code shared between test files
has to live somewhere else: an ordinary module, reached the way `tests.fixtures` was above.

## Packages above a test file

A package's `__init__.py` is imported the same synthetic way, ahead of the test file itself, so a
`velox.use(...)` call in it reaches every test below. That walk stops at the first directory with
no `__init__.py`, independent of the rootdir search above it.

## Isolated tests

A `@velox.isolated` test runs in its own subprocess, which repeats the rootdir-on-`sys.path` step
before importing anything, so its imports behave exactly like the parent run's.

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

Both are ordinary absolute imports, resolved because `tests/` sits under the rootdir on `sys.path`
— not because either file is a collected test module.
