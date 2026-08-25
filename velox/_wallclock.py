"""The run's start instant, taken from the OS process start rather than from any
point in velox's own code.

Imported before the rest of the package so `PROCESS_START` is fixed as early as the
interpreter allows on platforms without a readable process start time.
"""

from __future__ import annotations

import os
import time


def _elapsed_since_process_start() -> float | None:
    """Seconds this process has been alive, or `None` where the OS won't say.

    Reads field 22 of `/proc/self/stat` (start time, in clock ticks since boot) and
    compares it against `CLOCK_BOOTTIME`, which counts from the same origin. The
    comm field can itself contain spaces and parentheses, so the split is anchored on
    the *last* `") "` in the line rather than on whitespace.
    """
    try:
        stat = open("/proc/self/stat", encoding="utf-8").read()  # noqa: SIM115
        ticks = float(stat.rsplit(") ", 1)[1].split()[19])
        started = ticks / os.sysconf("SC_CLK_TCK")
        return time.clock_gettime(time.CLOCK_BOOTTIME) - started
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _process_start() -> float:
    age = _elapsed_since_process_start()
    return time.monotonic() - age if age is not None else time.monotonic()


PROCESS_START = _process_start()
"""`time.monotonic()`-comparable instant this process began.

Comparable with `time.monotonic()`, not with wall-clock time, and carries the cost of
interpreter startup and of importing velox -- both of which a `time velox` reading
includes and no timer started inside `main` can see.
"""
