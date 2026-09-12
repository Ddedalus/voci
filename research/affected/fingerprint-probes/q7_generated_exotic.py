"""Q7: generated code / exec / <string> filenames, .pyc-only modules, zipimport,
and C-implemented callables -- what co_filename shows up, and how should a
first-party/third-party/ignore classifier treat each.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
TOOL_ID = 3

ROOTDIR = Path("/home/hubert/voci")


def trace(fn):
    records = []

    def on_start(code, off):
        records.append((code.co_filename, code.co_qualname))

    sys.monitoring.use_tool_id(TOOL_ID, "q7-probe")
    sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.PY_START, on_start)
    sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.PY_START)
    try:
        fn()
    finally:
        sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.NO_EVENTS)
        sys.monitoring.free_tool_id(TOOL_ID)
    return records


def classify(filename: str) -> str:
    """A sketch of the classifier the tracer would need."""
    if filename in ("<string>", "<stdin>", ""):
        return "no-real-file (generated / exec / repl)"
    if filename.startswith("<") and filename.endswith(">"):
        return "synthetic-but-named (e.g. attrs generated methods, frozen stdlib)"
    p = Path(filename)
    try:
        p.exists()
    except OSError:
        return "unresolvable path"
    if ".zip" in filename or "zipimport" in filename:
        return "zip-packed"
    if not p.exists():
        return "path-does-not-exist-on-disk (pyc-only / zipimport / deleted)"
    try:
        p.relative_to(ROOTDIR)
        under_root = True
    except ValueError:
        under_root = False
    if under_root and "site-packages" not in filename and ".venv" not in filename:
        return "first-party (under rootdir, not a venv)"
    if "site-packages" in filename or ".venv" in filename:
        return "third-party (installed package)"
    return "other (outside rootdir, not a venv)"


def case_exec_string():
    def run():
        src = "def f():\n    return 1\nf()\n"
        exec(compile(src, "<string>", "exec"), {})

    return trace(run)


def case_exec_custom_label():
    def run():
        src = "def f():\n    return 1\nf()\n"
        exec(compile(src, "<generated:my_plugin>", "exec"), {})

    return trace(run)


def case_zipimport():
    def run():
        sys.path.insert(0, str(Path(__file__).parent / "zippkg" / "lib.zip"))
        try:
            import zmod

            zmod.zfunc()
        finally:
            sys.path.pop(0)
            sys.modules.pop("zmod", None)

    return trace(run)


def case_pyc_only():
    def run():
        sys.path.insert(0, str(Path(__file__).parent / "pyc_only"))
        try:
            import pmod

            pmod.pfunc()
        finally:
            sys.path.pop(0)
            sys.modules.pop("pmod", None)

    return trace(run)


def case_c_callable():
    def run():
        # sorted(), len(), str.upper are C-implemented -- no Python frame, so no
        # PY_START at all for the call itself. A Python function called FROM
        # C (e.g. via list.sort(key=...)) still fires PY_START normally.
        sorted([3, 1, 2])
        len("abc")
        "abc".upper()

        def key_fn(x):
            return -x

        sorted([3, 1, 2], key=key_fn)

    return trace(run)


def main():
    print("== exec with filename='<string>' ==")
    for f, q in case_exec_string():
        print(f"  {f!r:30} {q!r:10} -> {classify(f)}")

    print("\n== exec with a custom synthetic filename ==")
    for f, q in case_exec_custom_label():
        print(f"  {f!r:30} {q!r:10} -> {classify(f)}")

    print("\n== zipimport ==")
    for f, q in case_zipimport():
        if "zmod" in f or "zip" in f:
            print(f"  {f!r:60} {q!r:10} -> {classify(f)}")

    print("\n== pyc-only module (source .py deleted after compiling) ==")
    for f, q in case_pyc_only():
        if "pmod" in f:
            print(f"  {f!r:60} {q!r:10} -> {classify(f)}")

    print("\n== C-implemented callables (sorted/len/str.upper) + a Python key fn ==")
    recs = case_c_callable()
    print(f"  total PY_START records for this block: {len(recs)}")
    for f, q in recs:
        print(f"  {f!r:30} {q!r:10} -> {classify(f)}")
    print(
        "  sorted()/len()/str.upper() themselves never appear -- PY_START fires only for\n"
        "  Python frames, so a dependency mediated entirely by C code (e.g. a C extension\n"
        "  calling back into a *different* first-party module than the one visible in the\n"
        "  Python-level call stack) would be invisible to this tracer. key_fn IS visible,\n"
        "  because CPython still calls it as a normal Python frame even though the sort\n"
        "  itself is C."
    )


if __name__ == "__main__":
    main()
