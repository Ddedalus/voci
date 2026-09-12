"""Minimal child-process tracer, installed via a .pth import.

Does nothing unless VOCI_AFFECTED_DIR is set in the environment. When set,
installs a sys.monitoring PY_START callback that records (co_filename,
co_qualname) for first-party files (those under VOCI_ROOT), and flushes to
VOCI_AFFECTED_DIR/<pid>.json at process exit. Also writes a "started" marker
immediately so a parent process can tell a child launched even if it never
got to flush (killed, os._exit, wrong interpreter never even imports this).
"""
from __future__ import annotations

import os

_AFFECTED_DIR = os.environ.get("VOCI_AFFECTED_DIR")

if _AFFECTED_DIR:
    import atexit
    import json
    import sys
    import time

    _ROOT = os.environ.get("VOCI_ROOT", os.getcwd())
    _TEST_ID = os.environ.get("VOCI_TEST_ID", "<unknown>")
    _PID = os.getpid()

    # Started marker, written before anything else can go wrong.
    try:
        os.makedirs(_AFFECTED_DIR, exist_ok=True)
        with open(os.path.join(_AFFECTED_DIR, f"{_PID}.started"), "w") as f:
            f.write(json.dumps({"pid": _PID, "test_id": _TEST_ID, "t": time.time()}))
    except OSError:
        pass

    _seen: set[tuple[str, str]] = set()
    _classify_cache: dict[str, bool] = {}

    def _is_first_party(filename: str) -> bool:
        cached = _classify_cache.get(filename)
        if cached is not None:
            return cached
        result = filename.startswith(_ROOT) and filename.endswith(".py")
        _classify_cache[filename] = result
        return result

    _TOOL_ID = None

    def _register() -> int | None:
        import sys as _sys

        for tool_id in (3, 4, 5):
            try:
                if _sys.monitoring.get_tool(tool_id) is None:
                    _sys.monitoring.use_tool_id(tool_id, "voci-child-tracer")
                    return tool_id
            except ValueError:
                continue
        return None

    def _py_start(code, instruction_offset):  # noqa: ANN001
        filename = code.co_filename
        if not _is_first_party(filename):
            return sys.monitoring.DISABLE
        _seen.add((filename, code.co_qualname))
        return None

    def _flush() -> None:
        try:
            path = os.path.join(_AFFECTED_DIR, f"{_PID}.json")
            with open(path, "w") as f:
                json.dump(
                    {
                        "pid": _PID,
                        "ppid": os.getppid(),
                        "test_id": _TEST_ID,
                        "seen": sorted(_seen),
                    },
                    f,
                )
            # Finished marker, separate from the started one.
            with open(os.path.join(_AFFECTED_DIR, f"{_PID}.finished"), "w") as f:
                f.write("1")
        except OSError:
            pass

    _TOOL_ID = _register()
    if _TOOL_ID is not None:
        sys.monitoring.set_events(_TOOL_ID, sys.monitoring.events.PY_START)
        sys.monitoring.register_callback(
            _TOOL_ID, sys.monitoring.events.PY_START, _py_start
        )
        atexit.register(_flush)

        # os._exit() bypasses atexit (this is exactly why multiprocessing's
        # fork-mode bootstrap loses coverage.py data too -- its _bootstrap
        # finally-block calls os._exit(), not sys.exit()). Patch it so a
        # flush happens first, mirroring coverage.py's `patch = _exit`.
        _real_os_exit = os._exit

        def _patched_os_exit(status):  # noqa: ANN001
            try:
                _flush()
            except Exception:
                pass
            _real_os_exit(status)

        os._exit = _patched_os_exit

        def _after_fork_child() -> None:
            # Re-key the marker files and reset per-process state so a forked
            # child doesn't silently share/overwrite the parent's records.
            global _PID, _seen
            _PID = os.getpid()
            _seen = set()
            try:
                with open(os.path.join(_AFFECTED_DIR, f"{_PID}.started"), "w") as f:
                    f.write(json.dumps({"pid": _PID, "test_id": _TEST_ID, "forked": True}))
            except OSError:
                pass

        os.register_at_fork(after_in_child=_after_fork_child)
