"""`python -m velox`: `cli.main`, reachable without the `velox` console script on `PATH`.

The spelling a tool wrapping a whole run needs -- `coverage run -m velox`.
"""

from __future__ import annotations

import sys

from velox.cli import main

if __name__ == "__main__":
    sys.exit(main())
