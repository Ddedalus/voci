"""A stand-in `env_key` for `driver.py`'s `store_record` calls, until M4 builds the real one.

The real key covers interpreter implementation/version/platform, every entry-point group queried
during the run, the resolved `[tool.voci]` plus `-W`/`--timeout`/concurrency/assert mode,
`LANG`/`LC_*`/`TZ`, and hashes of extension modules loaded from under rootdir -- a mismatch means a
full run, so being too coarse only costs extra full runs, never an unsound skip; that's the whole
reason this placeholder is safe to ship ahead of the real one. It covers only the first component
-- interpreter implementation, full version, and `sys.platform` -- which is already enough to keep
an interpreter switch (`just py run 3.13 ...` against the default 3.14) from sharing records that
were never valid across the two. Everything the real key adds beyond that (a `[tool.voci]` edit, a
plugin install, a locale change, ...) simply costs a full run under this placeholder where M4's own
key would have re-used a still-matching record instead -- always the safe direction to be wrong in.
"""

from __future__ import annotations

import platform
import sys

__all__ = ["placeholder_env_key"]


def placeholder_env_key() -> str:
    return f"{sys.implementation.name}-{platform.python_version()}-{sys.platform}"
