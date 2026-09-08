"""`python -m voci`: `cli.main`, reachable without the `voci` console script on `PATH`.

The spelling a tool wrapping a whole run needs -- `coverage run -m voci`.
"""

from __future__ import annotations

import sys

from voci.cli import main

if __name__ == "__main__":
    sys.exit(main())
