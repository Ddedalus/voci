"""Q3: what does a per-function source hash need to cover?

For each edit type, build a "before" and "after" module source, and check whether
three candidate fingerprints change:

  FUNC_SRC   = hash(ast.get_source_segment(function))         # naive function-only
  FUNC_DUMP  = hash(ast.dump(function_node))                    # AST-shape, no positions
  RESIDUAL   = hash(module source with every function/class body blanked to `pass`,
                     i.e. everything BUT function bodies: imports, class attrs,
                     decorators-as-written, defaults-as-written, base classes, module
                     constants)

A "caught" edit is one where a scheme that's supposed to catch it actually changes.
"""

import ast
import hashlib
import textwrap


def h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def func_src_hash(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(src, node)
            return h(seg)
    raise LookupError(name)


def func_dump_hash(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return h(ast.dump(node, annotate_fields=True, include_attributes=False))
    raise LookupError(name)


def module_residual(src: str) -> str:
    """Module source with every function/class body collapsed, decorators and
    signatures kept as-is (a def line + `pass`), class bodies kept only as their
    class-level assignments/bases (no methods)."""
    tree = ast.parse(src)

    class Blanker(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            node.body = [ast.Pass()]
            node.decorator_list = node.decorator_list  # kept: decorator is part of residual
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            new_body = []
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    stmt.body = [ast.Pass()]
                    new_body.append(stmt)
                else:
                    new_body.append(stmt)
            node.body = new_body
            self.generic_visit(node)
            return node

    residual_tree = Blanker().visit(tree)
    ast.fix_missing_locations(residual_tree)
    return h(ast.dump(residual_tree, annotate_fields=True, include_attributes=False))


def module_residual_excluding_bodies_entirely(src: str) -> str:
    """Alternative residual: function bodies removed from the hash input by
    dumping everything except FunctionDef.body / AsyncFunctionDef.body, i.e.
    decorators + signature + defaults are IN, body is OUT, so this is exactly
    'whole module minus function bodies' as a single hash, structurally
    equivalent to module_residual above (kept for clarity, same result)."""
    return module_residual(src)


CASES = {}


def case(name, before, after, note):
    CASES[name] = (textwrap.dedent(before), textwrap.dedent(after), note)


case(
    "change_decorator",
    """
    def deco(f):
        return f

    @deco
    def target():
        return 1
    """,
    """
    def deco(f):
        def wrapper(*a, **kw):
            return f(*a, **kw)
        return wrapper

    @deco
    def target():
        return 1
    """,
    "decorator's OWN definition changed elsewhere; target()'s source text is identical",
)

case(
    "change_default_arg",
    """
    def target(x=1):
        return x
    """,
    """
    def target(x=2):
        return x
    """,
    "default value literal changed in the def line itself",
)

case(
    "change_dataclass_field",
    """
    from dataclasses import dataclass

    @dataclass
    class Point:
        x: int
        y: int
    """,
    """
    from dataclasses import dataclass

    @dataclass
    class Point:
        x: int
        y: int
        z: int = 0
    """,
    "class field changed; generated __init__ has no source at all to hash",
)

case(
    "change_module_constant",
    """
    LIMIT = 10

    def target(x):
        return x < LIMIT
    """,
    """
    LIMIT = 20

    def target(x):
        return x < LIMIT
    """,
    "target()'s own text unchanged; the constant it reads at call time moved",
)

case(
    "change_import_source",
    """
    from mod_x import helper

    def target(v):
        return helper(v)
    """,
    """
    from mod_z import helper

    def target(v):
        return helper(v)
    """,
    "target()'s text identical; which module `helper` binds to changed",
)

case(
    "change_base_class",
    """
    class Base1:
        def greet(self):
            return "base1"

    class Target(Base1):
        pass
    """,
    """
    class Base2:
        def greet(self):
            return "base2"

    class Target(Base2):
        pass
    """,
    "Target's own body (`pass`) is unchanged; its base class swapped",
)

case(
    "change_slots",
    """
    class Target:
        __slots__ = ("a",)

        def set_a(self, v):
            self.a = v
    """,
    """
    class Target:
        __slots__ = ("a", "b")

        def set_a(self, v):
            self.a = v
    """,
    "set_a's body unchanged; __slots__ (a class-body statement, not a function) changed",
)

case(
    "change_type_annotation_runtime_read",
    """
    def target(x: int):
        return x
    """,
    """
    def target(x: str):
        return x
    """,
    "annotation changed but is inside the def line -- captured by function source, "
    "unlike the dataclass-field case where the annotation lives in a class body statement",
)

case(
    "comment_only",
    """
    def target(x):
        return x + 1
    """,
    """
    def target(x):
        # comment
        return x + 1
    """,
    "pure comment insertion inside the function",
)


def main():
    print(f"{'edit':30} {'FUNC_SRC':10} {'FUNC_DUMP':10} {'MODULE_RESIDUAL':16} note")
    rows = []
    for name, (before, after, note) in CASES.items():
        target = "target" if "class Target" not in before or name in (
            "change_base_class", "change_slots",
        ) else "target"
        # figure out which function to hash: everything defines `target` except the
        # decorator/dataclass cases which still have `target`/`Point.__init__`-ish shape
        fn_name = "target" if "def target" in before else None
        if fn_name:
            fs_before = func_src_hash(before, fn_name)
            fs_after = func_src_hash(after, fn_name)
            fd_before = func_dump_hash(before, fn_name)
            fd_after = func_dump_hash(after, fn_name)
            func_src_changed = fs_before != fs_after
            func_dump_changed = fd_before != fd_after
        else:
            func_src_changed = None
            func_dump_changed = None

        mr_before = module_residual(before)
        mr_after = module_residual(after)
        residual_changed = mr_before != mr_after

        rows.append((name, func_src_changed, func_dump_changed, residual_changed, note))

    for name, fs, fd, mr, note in rows:
        def fmt(v):
            if v is None:
                return "n/a(no fn)"
            return "CHANGED" if v else "same"

        print(f"{name:30} {fmt(fs):10} {fmt(fd):10} {fmt(mr):16} {note}")


def decorator_attribution_probe():
    """The change_decorator row above shows deco()'s OWN body-hash catches the
    edit -- but only for a test that has `deco` in its per-test PY_START record.
    `@deco` above `target` calls deco(target) exactly once, at decoration time
    during module import. Trace it under two tests: one imports the module
    (and so triggers decoration), the other runs later against the
    already-imported module and only calls target()."""
    import sys

    before, after, _ = CASES["change_decorator"]
    TOOL_ID = 3

    module_ns = {}

    def trace_calling(src, mod_name, filename, do_import, do_call):
        records = []

        def on_start(code, off):
            if code.co_filename == filename:
                records.append(code.co_qualname)

        sys.monitoring.use_tool_id(TOOL_ID, "q3-deco-probe")
        sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.PY_START, on_start)
        sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.PY_START)
        try:
            if do_import:
                exec(compile(src, filename, "exec"), module_ns)
            if do_call:
                module_ns["target"]()
        finally:
            sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.NO_EVENTS)
            sys.monitoring.free_tool_id(TOOL_ID)
        return records

    # Test A: imports the module fresh (triggers `deco(target)` at decoration
    # time) and also calls target().
    recs_a = trace_calling(before, "deco_probe_mod", "deco_probe.py", do_import=True, do_call=True)
    # Test B: module already imported by test A in this process; B only calls
    # target(), same as voci tracing two tests in one worker process.
    recs_b = trace_calling(before, "deco_probe_mod", "deco_probe.py", do_import=False, do_call=True)

    print()
    print("== decorator attribution across two tests sharing one process ==")
    print(f"  test A (imports + calls) recorded qualnames: {sorted(set(recs_a))}")
    print(f"  test B (calls only, module already imported) recorded qualnames: {sorted(set(recs_b))}")
    print(
        "  -> only test A's per-test map contains 'deco'; a change to deco()'s OWN body\n"
        "     is invisible to test B's map under a scheme that unions 'per-test recorded\n"
        "     qualname -> function hash', because test B never recorded 'deco' at all.\n"
        "     Whole-file module-residual (as narrowly defined above: 'module source minus\n"
        "     every function body') does NOT save this case either, since deco's body is a\n"
        "     function body and is excluded from residual by construction.\n"
        "     This is the module-level-code risk (plan's risk #2) generalized: it is not just\n"
        "     <module>'s own top-level statements that run once at import and get attributed\n"
        "     to a single test -- ANY code that executes only as a side effect of import\n"
        "     (decorator application, dataclass/attrs class processing, metaclass __new__,\n"
        "     __init_subclass__) has the same one-shot-attribution problem."
    )


if __name__ == "__main__":
    main()
    decorator_attribution_probe()
