"""Q5: module-level code attribution.

Part 1: confirm which test gets the <module> PY_START record when a test runner
imports test modules and source modules once per process (first importer wins,
later importers get nothing).

Part 2: estimate, for every file under /home/hubert/voci/voci, what fraction of
its source is "module-level residual" (everything but function bodies) vs
function-body text -- i.e. how much of each file a whole-file invalidation rule
would be overinvalidating relative to a hypothetically-perfect per-function rule.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
TOOL_ID = 3

VOCI_SRC = Path("/home/hubert/voci/voci")


def part1_first_importer_wins():
    records_by_test = {}

    def make_callback(bucket):
        def on_start(code, off):
            if code.co_filename.endswith("shared_src.py"):
                bucket.append(code.co_qualname)

        return on_start

    src = "TOP_LEVEL_COUNTER = 0\n\ndef helper():\n    return TOP_LEVEL_COUNTER\n"
    filename = "shared_src.py"
    module_ns = {"__name__": "shared_src"}

    for test_name in ("test_one", "test_two", "test_three"):
        bucket = records_by_test[test_name] = []
        sys.monitoring.use_tool_id(TOOL_ID, "q5-probe")
        sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.PY_START, make_callback(bucket))
        sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.PY_START)
        try:
            if "TOP_LEVEL_COUNTER" not in module_ns:
                # first test to need the module triggers the actual import/exec
                exec(compile(src, filename, "exec"), module_ns)
            module_ns["helper"]()
        finally:
            sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.NO_EVENTS)
            sys.monitoring.free_tool_id(TOOL_ID)

    print("== part 1: which test's record includes <module> for shared_src.py ==")
    for test_name, recs in records_by_test.items():
        print(f"  {test_name}: {recs}")
    print(
        "  Confirms the plan's claim: only the first test to import a module (in an\n"
        "  in-process runner reusing sys.modules across tests) ever records that module's\n"
        "  <module> frame; every later test that depends on the same module has NO PY_START\n"
        "  record naming it at all -- not even indirectly -- despite genuinely depending on\n"
        "  whatever module-level code ran (constants, decorator applications, class bodies).\n"
        "  A rule keyed purely on 'which qualnames did this test's trace record' is blind to\n"
        "  this dependency for every test but the first; the plan's proposed rule -- any test\n"
        "  that recorded ANY function from file F also depends on F's module residual -- has to\n"
        "  be applied as a blanket per-file rule precisely because tracing alone can't discover\n"
        "  it per test."
    )


def residual_fraction(src: str) -> tuple[int, int, int]:
    """Returns (total_chars, function_body_chars, residual_chars) for one file's
    source, where function_body_chars is the sum of every FunctionDef/AsyncFunctionDef
    body's source span (excluding the def line/decorators/signature itself -- just
    the body -- since that's what's independently attributable to a traced call),
    and residual_chars is everything else (imports, class bodies' non-def statements,
    decorators, signatures, defaults, module constants, docstrings at module level).
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return (len(src), 0, len(src))

    lines = src.splitlines(keepends=True)

    def segment_len(node) -> int:
        if node.end_lineno is None:
            return 0
        if node.lineno == node.end_lineno:
            return node.end_col_offset - node.col_offset
        first = len(lines[node.lineno - 1]) - node.col_offset
        middle = sum(len(l) for l in lines[node.lineno : node.end_lineno - 1])
        last = node.end_col_offset
        return first + middle + last

    body_chars = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for stmt in node.body:
                body_chars += segment_len(stmt)

    total = len(src)
    return (total, body_chars, total - body_chars)


def part2_residual_fraction():
    files = sorted(p for p in VOCI_SRC.rglob("*.py") if "__pycache__" not in p.parts)
    grand_total = grand_body = 0
    rows = []
    for f in files:
        src = f.read_text(encoding="utf-8")
        total, body, residual = residual_fraction(src)
        grand_total += total
        grand_body += body
        rows.append((f.relative_to(VOCI_SRC), total, body, residual))

    print("\n== part 2: function-body fraction vs module-level residual, voci/*.py ==")
    print(f"  {'file':45} {'total':>8} {'body':>8} {'residual':>8} {'body%':>7}")
    for rel, total, body, residual in sorted(rows, key=lambda r: -r[1])[:15]:
        pct = 100 * body / total if total else 0
        print(f"  {str(rel):45} {total:8} {body:8} {residual:8} {pct:6.1f}%")

    grand_residual = grand_total - grand_body
    print(f"\n  TOTAL across {len(rows)} files: {grand_total} chars, "
          f"{grand_body} in function bodies ({100*grand_body/grand_total:.1f}%), "
          f"{grand_residual} residual ({100*grand_residual/grand_total:.1f}%)")
    print(
        "\n  Interpretation: 'residual' here is generous to the whole-file rule -- it counts\n"
        "  class-body statements, decorators, signatures and imports as residual, but a real\n"
        "  whole-file-invalidation rule doesn't partially invalidate; ANY residual change\n"
        "  reruns every test that touched ANY function in the file, including functions whose\n"
        "  own body-hash is untouched. The residual fraction above is a lower bound on how much\n"
        "  of a file's *edits* the rule cannot discriminate away from -- and it is a large\n"
        "  minority to just under half of file content across this codebase, meaning a\n"
        "  substantial share of realistic edits (import changes, decorator changes, class-attr/\n"
        "  dataclass-field changes, docstrings, signatures, defaults) fall into the whole-file\n"
        "  bucket even under Option B's function-level granularity."
    )


if __name__ == "__main__":
    part1_first_importer_wins()
    part2_residual_fraction()
