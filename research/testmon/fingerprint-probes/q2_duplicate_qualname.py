"""Q2: two functions with the same qualname in one file - how to disambiguate,
and what breaks when lines shift.
"""

import ast
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
TOOL_ID = 3

SRC_V1 = textwrap.dedent('''
    import typing

    if True:
        def dup():
            return "branch-true"
    else:
        def dup():
            return "branch-false"


    def dup():                 # plain redefinition, wins at runtime
        return "second-def"


    @typing.overload
    def stub(x: int) -> int: ...
    @typing.overload
    def stub(x: str) -> str: ...
    def stub(x):
        return x
''')

# Same logical functions, shifted down by inserting two blank lines up top,
# and the second `dup` grew a comment line above it.
SRC_V2 = textwrap.dedent('''


    import typing

    if True:
        def dup():
            return "branch-true"
    else:
        def dup():
            return "branch-false"


    # a comment that pushes the next def down
    def dup():                 # plain redefinition, wins at runtime
        return "second-def"


    @typing.overload
    def stub(x: int) -> int: ...
    @typing.overload
    def stub(x: str) -> str: ...
    def stub(x):
        return x
''')


def record_py_start(mod_name, src, filename):
    records = []

    def on_start(code, offset):
        if code.co_filename == filename:
            records.append((code.co_qualname, code.co_firstlineno))

    sys.modules.pop(mod_name, None)
    sys.monitoring.use_tool_id(TOOL_ID, "q2-probe")
    sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.PY_START, on_start)
    sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.PY_START)
    try:
        code_obj = compile(src, filename, "exec")
        ns = {"__name__": mod_name}
        exec(code_obj, ns)
        # call every def-shaped name that survived module exec, plus stub overload dispatch
        ns["dup"]()
        ns["stub"](1)
    finally:
        sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.NO_EVENTS)
        sys.monitoring.free_tool_id(TOOL_ID)
    return records


def ast_defs_by_qualname(src):
    """All FunctionDef nodes at module level (ignoring nested scope tracking for
    this narrow probe), keyed by name, in source order, with lineno."""
    tree = ast.parse(src)
    out = {}

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            out.setdefault(node.name, []).append(node.lineno)
            self.generic_visit(node)

        def visit_If(self, node):
            self.generic_visit(node)

    V().visit(tree)
    return out


def main():
    print("== v1 ==")
    recs1 = record_py_start("dupmod1", SRC_V1, "dup_v1.py")
    for qn, ln in recs1:
        print(f"  runtime PY_START: qualname={qn!r} firstlineno={ln}")
    defs1 = ast_defs_by_qualname(SRC_V1)
    for name, lines in defs1.items():
        print(f"  AST def {name!r} at lines {lines}")

    print()
    print("== v2 (shifted) ==")
    recs2 = record_py_start("dupmod2", SRC_V2, "dup_v2.py")
    for qn, ln in recs2:
        print(f"  runtime PY_START: qualname={qn!r} firstlineno={ln}")
    defs2 = ast_defs_by_qualname(SRC_V2)
    for name, lines in defs2.items():
        print(f"  AST def {name!r} at lines {lines}")

    print()
    print("== disambiguation check: (qualname, firstlineno) as key ==")
    print("v1 dup() bodies at distinct lines:", defs1["dup"])
    print("v1 stub() bodies (overload stubs + impl) at distinct lines:", defs1["stub"])
    print()
    print("Claim: a stub whose body is `...` never executes -> never fires PY_START,")
    print("so only the *last* stub/impl with a given qualname is ever observed running.")
    print("But the AST-fingerprint side must still hash all of them (an edit to a stub")
    print("signature is a real change even though it never runs), which (qualname,")
    print("firstlineno) as the fingerprint key handles fine since each stub has its own line.")
    print()
    print("Line-shift check: does firstlineno alone survive an unrelated edit above it?")
    shifted = [ln for ln in defs2["dup"]]
    print(f"  v1 dup() linenos: {defs1['dup']}  ->  v2 (2 blank lines + 1 comment added): {shifted}")
    print("  Every line number describing a *later* def moved --" if shifted != defs1["dup"] else "  unchanged --",
          "if the fingerprint key is (qualname, firstlineno) and the recorded PY_START used the OLD")
    print("  firstlineno, the post-edit record no longer matches any AST def in the new source:")
    print("  a same-qualname pair keyed by line becomes two different keys across a run boundary,")
    print("  even though the function's *body* is byte-identical -- an ordinary spurious invalidation,")
    print("  not a silent miss (a totally unrelated edit above it forces a re-run).")


if __name__ == "__main__":
    main()
