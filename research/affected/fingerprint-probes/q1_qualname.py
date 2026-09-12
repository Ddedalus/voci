"""Q1: does an AST-computed qualname match co_qualname for every PY_START record?

Builds qualnames the way CPython's compiler does (tracking class vs function scope
nesting, `<locals>` for nested defs, `<lambda>` for lambdas), then imports and
exercises sample_mod.py under a PY_START sys.monitoring callback and diffs the two
sets.
"""

import ast
import sys
import sysconfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

TOOL_ID = 3


class QualnameWalker(ast.NodeVisitor):
    """Reproduces CPython's compile-time qualname assignment via AST."""

    def __init__(self):
        # stack of (name, is_function) frames. Function frames contribute
        # "<locals>" to the qualname of names defined inside them; class frames
        # contribute just their name.
        self.stack: list[tuple[str, bool]] = []
        self.results: dict[tuple[str, int], str] = {}  # (name, lineno) -> qualname

    def _qualname_for(self, name: str) -> str:
        parts = []
        for frame_name, is_func in self.stack:
            parts.append(frame_name)
            if is_func:
                parts.append("<locals>")
        parts.append(name)
        return ".".join(parts)

    def _visit_function(self, node, name):
        qn = self._qualname_for(name)
        self.results[(name, node.lineno)] = qn
        self.stack.append((name, True))
        # type params (PEP 695) create their own annotation-scope frame, but
        # nested defs inside the body still see the function's own frame as
        # parent (checked empirically below), so we don't push a separate one.
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_function(node, node.name)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node, node.name)

    def visit_Lambda(self, node):
        self._visit_function(node, "<lambda>")

    def visit_ClassDef(self, node):
        qn = self._qualname_for(node.name)
        self.results[(node.name, node.lineno)] = qn
        self.stack.append((node.name, False))
        self.generic_visit(node)
        self.stack.pop()

    def _visit_comp(self, node, kind):
        # Comprehensions get their own code object pre-3.12 (and genexprs
        # always do); CPython 3.12+ inlines list/set/dict comps into the
        # enclosing frame (no separate code object, no PY_START). We still
        # record what the *name* would be, to compare against reality.
        qn = self._qualname_for(f"<{kind}>")
        self.results[(f"<{kind}>@{node.lineno}", node.lineno)] = qn
        self.generic_visit(node)

    def visit_ListComp(self, node):
        self._visit_comp(node, "listcomp")

    def visit_SetComp(self, node):
        self._visit_comp(node, "setcomp")

    def visit_DictComp(self, node):
        self._visit_comp(node, "dictcomp")

    def visit_GeneratorExp(self, node):
        self._visit_comp(node, "genexpr")

    def visit_TypeAlias(self, node):
        self.generic_visit(node)


def ast_qualnames(source: str) -> dict[tuple[str, int], str]:
    tree = ast.parse(source)
    w = QualnameWalker()
    w.visit(tree)
    return w.results


def main():
    stdlib = sysconfig.get_paths()["stdlib"]

    records: list[tuple[str, str, int]] = []  # (filename, qualname, firstlineno)

    def on_start(code, instruction_offset):
        records.append((code.co_filename, code.co_qualname, code.co_firstlineno))

    sys.monitoring.use_tool_id(TOOL_ID, "q1-probe")
    sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.PY_START, on_start)
    sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.PY_START)

    try:
        # Import *inside* the monitored region: the <module> code object and
        # every class-body / decorator application it runs at import time only
        # fires PY_START if the callback is already armed.
        import sample_mod

        # Exercise everything so all code objects actually run at least once.
        sample_mod.plain_function(1)
        sample_mod.outer()()
        sample_mod.closure_maker(1)(2)
        sample_mod.make_lambda(2)
        sample_mod.has_comprehensions()
        list(sample_mod.generator_function())
        import asyncio

        asyncio.run(sample_mod.async_function())

        async def drain_agen():
            return [x async for x in sample_mod.async_generator()]

        asyncio.run(drain_agen())
        sample_mod.decorated_function(1)
        sample_mod.decorated_elsewhere()
        o = sample_mod.Outer()
        o.method()
        o.method_with_nested()
        _ = o.prop
        sample_mod.Outer.cls_method()
        sample_mod.Outer.static_method()
        sample_mod.Outer.Inner().inner_method()
        sample_mod.Outer.Inner.InnerInner().deepest()
        sample_mod.Point(1, 2)
        p = sample_mod.make_partial()
        p()
        sample_mod.overloaded(1)
        sample_mod.uses_match(0)
        sample_mod.uses_match([1, 2])
        sample_mod.uses_match("x")
        sample_mod.generic_function(1)
        sample_mod.GenericClass().method(1)
        sample_mod.has_annotation(1)
        # PEP 649 lazy annotations (3.14+ only -- `__annotate__` doesn't exist
        # as a function attribute pre-3.14): force evaluation if present.
        ann = getattr(sample_mod.has_annotation, "__annotate__", None)
        if ann is not None:
            ann(1)  # Format.VALUE = 1
    finally:
        sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.NO_EVENTS)
        sys.monitoring.free_tool_id(TOOL_ID)

    src_path = Path(sample_mod.__file__)
    ast_qn = ast_qualnames(src_path.read_text())
    ast_qualname_set = set(ast_qn.values())
    # also add module-level scope name (no AST node covers <module>)
    ast_qualname_set.add("<module>")

    own_records = [r for r in records if r[0] == str(src_path)]
    seen_qualnames = {r[1] for r in own_records}

    print(f"Total PY_START records: {len(records)}")
    print(f"Records from sample_mod.py: {len(own_records)}")
    print()
    print("== distinct (qualname, firstlineno) seen at runtime, from sample_mod.py ==")
    for filename, qualname, lineno in sorted(set(own_records), key=lambda r: r[2]):
        in_ast = qualname in ast_qualname_set
        print(f"  L{lineno:>4}  {qualname!r:45}  ast_match={in_ast}")

    print()
    print("== AST-derived qualnames never seen at runtime (not exercised or not real code objs) ==")
    for (name, lineno), qn in sorted(ast_qn.items(), key=lambda kv: kv[0][1]):
        if qn not in seen_qualnames:
            print(f"  L{lineno:>4}  {qn!r}")

    print()
    print("== runtime qualnames with NO AST match at all ==")
    for qualname in sorted(seen_qualnames - ast_qualname_set):
        print(f"  {qualname!r}")

    print()
    print("== filenames seen (other than sample_mod.py) ==")
    other_files = sorted({r[0] for r in records if r[0] != str(src_path)})
    for f in other_files:
        tag = "stdlib" if f.startswith(stdlib) else ("no-file" if f in ("<string>", "") else "other")
        print(f"  {tag:8} {f}")


if __name__ == "__main__":
    main()
