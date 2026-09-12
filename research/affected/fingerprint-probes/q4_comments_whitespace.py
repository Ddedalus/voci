"""Q4: comment-only edits -- should they invalidate? Compare hashing
ast.dump(node) (no attributes, no comments/whitespace) vs raw source text, and
time both over a real tree (oss/pytest/src).
"""

import ast
import hashlib
import time
from pathlib import Path

PYTEST_SRC = Path("/home/hubert/voci/oss/pytest/src")


def h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def demo_comment_sensitivity():
    src_a = "def f(x):\n    return x + 1\n"
    src_b = "def f(x):\n    # a comment\n    return x + 1\n"
    src_c = "def f(x):\n\n\n    return x + 1\n"  # blank lines only

    def dump_hash(src):
        tree = ast.parse(src)
        fn = tree.body[0]
        return h(ast.dump(fn, annotate_fields=True, include_attributes=False).encode())

    def text_hash(src):
        tree = ast.parse(src)
        fn = tree.body[0]
        seg = ast.get_source_segment(src, fn)
        return h(seg.encode())

    print("== comment/whitespace sensitivity (single function) ==")
    print(f"  {'variant':30} {'ast.dump hash':20} {'source-text hash':20}")
    for name, src in [("no comment", src_a), ("+ comment", src_b), ("+ blank lines", src_c)]:
        print(f"  {name:30} {dump_hash(src):20} {text_hash(src):20}")
    print(
        "  ast.dump ignores comments AND blank lines (they aren't nodes) -> both variants\n"
        "  hash identical to the baseline. Source-text hashing changes on either edit."
    )


def find_py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def timing_run():
    files = find_py_files(PYTEST_SRC)
    total_bytes = 0
    sources = []
    for f in files:
        try:
            src = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        sources.append((f, src))
        total_bytes += len(src)

    loc = sum(src.count("\n") for _, src in sources)
    print(f"\n== timing over oss/pytest/src: {len(sources)} files, {loc} LOC, {total_bytes/1024:.0f} KiB ==")

    # 1. ast.parse cost alone
    t0 = time.perf_counter()
    trees = []
    for _, src in sources:
        trees.append(ast.parse(src))
    t_parse = time.perf_counter() - t0

    # 2. text-hash cost: hash whole-file text (proxy for "module residual via text
    #    slicing" -- in practice you'd hash per function segment, but the total
    #    bytes hashed across all functions in a file approaches the whole file)
    t0 = time.perf_counter()
    for _, src in sources:
        h(src.encode())
    t_text_hash_whole_file = time.perf_counter() - t0

    # 3. ast.dump-hash cost: dump every function/class def node individually
    #    (this is the per-function granularity the plan actually needs) plus
    #    the module residual dump.
    def per_function_dump_hashes(tree):
        out = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(h(ast.dump(node, annotate_fields=True, include_attributes=False).encode()))
        return out

    t0 = time.perf_counter()
    total_funcs = 0
    for tree in trees:
        fh = per_function_dump_hashes(tree)
        total_funcs += len(fh)
    t_dump_hash = time.perf_counter() - t0

    # 4. per-function TEXT hash (ast.get_source_segment) for comparison
    def per_function_text_hashes(src, tree):
        out = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seg = ast.get_source_segment(src, node)
                if seg is not None:
                    out.append(h(seg.encode()))
        return out

    t0 = time.perf_counter()
    for (_, src), tree in zip(sources, trees):
        per_function_text_hashes(src, tree)
    t_text_per_fn = time.perf_counter() - t0

    # 5. optimized per-function text hash: split lines once per file, slice by
    #    (lineno, col_offset)-(end_lineno, end_col_offset) instead of calling
    #    ast.get_source_segment (which redoes the splitlines() internally
    #    every time it's called).
    def per_function_text_hashes_fast(src, tree):
        lines = src.splitlines(keepends=True)
        out = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.end_lineno is None:
                    continue
                if node.lineno == node.end_lineno:
                    seg = lines[node.lineno - 1][node.col_offset : node.end_col_offset]
                else:
                    first = lines[node.lineno - 1][node.col_offset :]
                    middle = lines[node.lineno : node.end_lineno - 1]
                    last = lines[node.end_lineno - 1][: node.end_col_offset]
                    seg = "".join([first, *middle, last])
                out.append(h(seg.encode()))
        return out

    t0 = time.perf_counter()
    for (_, src), tree in zip(sources, trees):
        per_function_text_hashes_fast(src, tree)
    t_text_per_fn_fast = time.perf_counter() - t0

    print(f"  ast.parse (cold, all files):               {t_parse*1000:8.1f} ms")
    print(f"  whole-file text hash (sha256, all files):   {t_text_hash_whole_file*1000:8.1f} ms")
    print(f"  per-function ast.dump + hash ({total_funcs} funcs): {t_dump_hash*1000:8.1f} ms")
    print(f"  per-function get_source_segment + hash:     {t_text_per_fn*1000:8.1f} ms")
    print(f"  per-function fast-slice text hash:          {t_text_per_fn_fast*1000:8.1f} ms")
    print(
        "\n  ast.dump+hash costs about as much as ast.parse itself on this tree.\n"
        "  ast.get_source_segment+hash is far more expensive than either -- it re-splits the file\n"
        "  into lines and re-slices on every call, so its cost is roughly O(functions x file_size)\n"
        "  rather than O(file_size); the naive text-hash implementation is the expensive choice\n"
        "  here, not the cheap one. Slicing from one cached splitlines() per file (fast-slice, above)\n"
        "  fixes that and comes out cheaper than dump+hash too. So: dump-hash buys comment/whitespace\n"
        "  insensitivity essentially for free relative to parse cost; a *correctly implemented*\n"
        "  text-hash is cheaper still but is comment/whitespace-sensitive by construction."
    )


if __name__ == "__main__":
    demo_comment_sensitivity()
    timing_run()
