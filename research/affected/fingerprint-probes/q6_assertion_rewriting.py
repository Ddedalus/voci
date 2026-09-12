"""Q6: does assertion rewriting change co_qualname / co_firstlineno / co_filename
of test functions, and does it introduce extra code objects that fire PY_START?
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, "/home/hubert/voci")

TOOL_ID = 3

TEST_SRC = '''
def helper(x):
    return x + 1


def test_simple():
    assert helper(1) == 2


def test_compound():
    a = [1, 2, 3]
    assert len(a) == 3 and a[0] == 1


class TestClass:
    def test_method(self):
        assert True

    def test_with_message(self):
        assert 1 == 2, "custom message"
'''


def trace_module(code_obj, filename):
    records = []

    def on_start(code, off):
        if code.co_filename == filename:
            records.append((code.co_qualname, code.co_firstlineno))

    sys.monitoring.use_tool_id(TOOL_ID, "q6-probe")
    sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.PY_START, on_start)
    sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.PY_START)
    ns = {}
    try:
        exec(code_obj, ns)
        # run every test_* function/method so its own frame fires
        for name, val in list(ns.items()):
            if name.startswith("test_") and callable(val):
                try:
                    val()
                except AssertionError:
                    pass
        for name, val in list(ns.items()):
            if isinstance(val, type) and name.startswith("Test"):
                inst = val()
                for attr in dir(inst):
                    if attr.startswith("test_"):
                        try:
                            getattr(inst, attr)()
                        except AssertionError:
                            pass
    finally:
        sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.NO_EVENTS)
        sys.monitoring.free_tool_id(TOOL_ID)
    return sorted(set(records), key=lambda r: r[1])


def main():
    filename = "test_probe_q6.py"

    # -- baseline: plain compile, no rewriting --
    tree_plain = ast.parse(TEST_SRC, filename=filename)
    code_plain = compile(tree_plain, filename, "exec")
    recs_plain = trace_module(code_plain, filename)

    # -- rewritten: run it through voci's actual rewriter, the same way
    #    _rewrite_test in the vendored module does it (dont_inherit=True, no
    #    fix_missing_locations -- the rewriter sets locations itself). --
    from voci._assertions._vendor.rewrite import rewrite_asserts
    from voci._assertions._vendor._shim import Config

    tree_rewrite = ast.parse(TEST_SRC, filename=filename)
    config = Config()
    rewrite_asserts(tree_rewrite, TEST_SRC.encode(), filename, config)
    code_rewritten = compile(tree_rewrite, filename, "exec", dont_inherit=True)
    recs_rewritten = trace_module(code_rewritten, filename)

    print("== baseline (no rewrite) PY_START records: (qualname, firstlineno) ==")
    for r in recs_plain:
        print(f"  {r}")

    print("\n== rewritten PY_START records: (qualname, firstlineno) ==")
    for r in recs_rewritten:
        print(f"  {r}")

    print("\n== diff ==")
    only_plain = set(recs_plain) - set(recs_rewritten)
    only_rewritten = set(recs_rewritten) - set(recs_plain)
    print(f"  only in baseline:  {sorted(only_plain)}")
    print(f"  only in rewritten: {sorted(only_rewritten)}")
    print(f"  identical: {set(recs_plain) == set(recs_rewritten)}")

    # co_filename check specifically
    def collect_filenames(code_obj, seen=None):
        seen = seen if seen is not None else set()
        seen.add(code_obj.co_filename)
        for const in code_obj.co_consts:
            if isinstance(const, type(code_obj)):
                collect_filenames(const, seen)
        return seen

    print(f"\n  filenames referenced by baseline code object:  {collect_filenames(code_plain)}")
    print(f"  filenames referenced by rewritten code object: {collect_filenames(code_rewritten)}")


if __name__ == "__main__":
    main()
