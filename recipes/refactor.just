# AST-based recipes for splitting/moving top-level objects and rewiring their imports
# across the repo. Invoked as `just refactor <recipe>` — see CLAUDE.md's refactor-tools skill.

set working-directory := '..'

# Install dependencies the refactor recipes need (ruff only — see plans/rationale.md)
init:
    uv add ruff --dev

# Summarize a python file's top-level objects, one per line, as `name:start,end`
summarize path:
    #!/usr/bin/env -S uv run python3
    import ast

    path = "{{path}}"
    src = open(path).read()
    body = ast.parse(src).body

    has_docstring = (
        bool(body)
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0].value, "value", None), str)
    )
    if has_docstring:
        print(f"__doc__:{body[0].lineno},{body[0].end_lineno}")

    imp_start = imp_end = None
    for n in body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            imp_start = n.lineno if imp_start is None else min(imp_start, n.lineno)
            imp_end = n.end_lineno if imp_end is None else max(imp_end, n.end_lineno)
    if imp_start is not None:
        print(f"__imports__:{imp_start},{imp_end}")

    for i, n in enumerate(body):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            continue
        if i == 0 and has_docstring:
            continue
        name, start = None, n.lineno
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = n.name
            if n.decorator_list:
                start = min(d.lineno for d in n.decorator_list)
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            name = n.targets[0].id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            name = n.target.id
        elif isinstance(n, ast.TypeAlias) and isinstance(n.name, ast.Name):
            name = n.name.id
        if name is not None:
            print(f"{name}:{start},{n.end_lineno}")

# Move object(s) from source to destination: copies all of source's imports into
# destination's import block, appends the objects, then ruffs both files clean.
move source destination +objects:
    #!/usr/bin/env -S uv run python3
    import ast
    import os
    import subprocess
    import sys

    source, dest = "{{source}}", "{{destination}}"
    objs = "{{objects}}".split()

    def leading_docstring_end(body):
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0].value, "value", None), str):
            return body[0].end_lineno
        return 0

    def name_of(n):
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return n.name
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            return n.targets[0].id
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            return n.target.id
        if isinstance(n, ast.TypeAlias) and isinstance(n.name, ast.Name):
            return n.name.id
        return None

    def start_of(n):
        decorators = getattr(n, "decorator_list", None)
        return min(d.lineno for d in decorators) if decorators else n.lineno

    src_text = open(source).read()
    src_lines = src_text.splitlines(keepends=True)
    src_body = ast.parse(src_text).body

    import_chunks, move_ranges, found = [], [], set()
    for n in src_body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            import_chunks.append("".join(src_lines[n.lineno - 1 : n.end_lineno]))
            continue
        name = name_of(n)
        if name in objs:
            move_ranges.append((start_of(n), n.end_lineno))
            found.add(name)

    missing = [o for o in objs if o not in found]
    if missing:
        sys.exit(f"error: {missing} not found at top level of {source}")

    move_ranges.sort()
    skip = {i for start, end in move_ranges for i in range(start - 1, end)}
    moved_text = "\n\n".join("".join(src_lines[s - 1 : e]).rstrip("\n") for s, e in move_ranges)
    copied_imports = "".join(import_chunks).rstrip("\n")

    open(source, "w").write("".join(l for i, l in enumerate(src_lines) if i not in skip))

    dest_text = open(dest).read() if os.path.exists(dest) else ""
    dest_lines = dest_text.splitlines(keepends=True)
    dest_body = ast.parse(dest_text).body if dest_text.strip() else []

    insert_at = leading_docstring_end(dest_body)
    for n in dest_body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            insert_at = max(insert_at, n.end_lineno)

    head = "".join(dest_lines[:insert_at]).rstrip("\n")
    tail = "".join(dest_lines[insert_at:]).strip("\n")
    parts = [p for p in (head, copied_imports, tail, moved_text) if p]
    open(dest, "w").write("\n\n".join(parts) + "\n")

    subprocess.run(["uv", "run", "ruff", "check", "--fix", "--unsafe-fixes", "--select", "I,F401", source, dest])
    subprocess.run(["uv", "run", "ruff", "format", source, dest], check=True)
    print(f"moved {objs} from {source} to {dest}")

# Rewire every from-import of `obj` across the repo: old_module -> new_module
rewire obj old_module new_module:
    #!/usr/bin/env -S uv run python3
    import ast
    import re
    import subprocess
    import sys
    from pathlib import Path

    obj, old_module, new_module = "{{obj}}", "{{old_module}}", "{{new_module}}"
    skip_dirs = {".git", ".venv", "__pycache__", "pytest", "fastapi", "research", "spec"}

    def py_files():
        for p in Path(".").rglob("*.py"):
            if not any(part in skip_dirs for part in p.parts):
                yield p

    touched, diffs = [], []
    for path in py_files():
        text = path.read_text()
        if old_module not in text or obj not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue

        # ast.walk, not tree.body: a nested import (inside `if TYPE_CHECKING:`, inside a
        # function body) is otherwise invisible here and silently left unrewired.
        target = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom)
                and n.module == old_module
                and any(a.name == obj for a in n.names)
            ),
            None,
        )
        if target is None:
            continue

        lines = text.splitlines(keepends=True)
        indent = re.match(r"[ \t]*", lines[target.lineno - 1]).group()
        old_stmt = "".join(lines[target.lineno - 1 : target.end_lineno]).rstrip("\n")
        # Only preserved when the import disappears entirely (single name, nothing left at
        # this location) -- attaching it to a shrunk multi-name import would be a guess.
        trailing_comment = ""
        comment_match = re.search(r"\s*(#.*)$", lines[target.end_lineno - 1].rstrip("\n"))
        if comment_match:
            trailing_comment = "  " + comment_match.group(1)

        alias = next(a for a in target.names if a.name == obj)
        remaining = [a for a in target.names if a.name != obj]
        new_line = f"{indent}from {new_module} import {obj}"
        if alias.asname:
            new_line += f" as {alias.asname}"
        if remaining:
            names = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in remaining)
            replacement = f"{indent}from {old_module} import {names}\n"
            new_line += "\n"
        else:
            replacement = ""
            new_line += f"{trailing_comment}\n"

        before = "".join(lines[: target.lineno - 1])
        after = "".join(lines[target.end_lineno :])
        path.write_text(before + replacement + new_line + after)
        touched.append(str(path))
        diffs.append((str(path), old_stmt, (replacement + new_line).rstrip("\n")))

    if not touched:
        print(f"no from-imports of {obj!r} from {old_module!r} found")
        sys.exit(0)

    subprocess.run(["uv", "run", "ruff", "check", "--fix", "--unsafe-fixes", "--select", "I,F401", *touched])
    subprocess.run(["uv", "run", "ruff", "format", *touched], check=True)
    print(f"rewired {obj} ({old_module} -> {new_module}) in {len(touched)} file(s):")
    for path_str, old_stmt, new_stmt in diffs:
        print(f"  {path_str}")
        for line in old_stmt.splitlines():
            print(f"    - {line}")
        for line in new_stmt.splitlines():
            print(f"    + {line}")

# Repoint a whole-module import (`from parent import name`) at a new submodule, keeping the
# same locally-bound alias so every existing `name.thing` call site elsewhere is untouched.
rewire-module parent name new_parent new_name:
    #!/usr/bin/env -S uv run python3
    import ast
    import re
    import subprocess
    import sys
    from pathlib import Path

    parent, name = "{{parent}}", "{{name}}"
    new_parent, new_name = "{{new_parent}}", "{{new_name}}"
    skip_dirs = {".git", ".venv", "__pycache__", "pytest", "fastapi", "research", "spec"}

    def py_files():
        for p in Path(".").rglob("*.py"):
            if not any(part in skip_dirs for part in p.parts):
                yield p

    touched, diffs = [], []
    for path in py_files():
        text = path.read_text()
        if parent not in text or name not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue

        target = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom)
                and n.module == parent
                and any(a.name == name for a in n.names)
            ),
            None,
        )
        if target is None:
            continue

        lines = text.splitlines(keepends=True)
        indent = re.match(r"[ \t]*", lines[target.lineno - 1]).group()
        old_stmt = "".join(lines[target.lineno - 1 : target.end_lineno]).rstrip("\n")
        trailing_comment = ""
        comment_match = re.search(r"\s*(#.*)$", lines[target.end_lineno - 1].rstrip("\n"))
        if comment_match:
            trailing_comment = "  " + comment_match.group(1)

        alias = next(a for a in target.names if a.name == name)
        bound_name = alias.asname or name
        remaining = [a for a in target.names if a.name != name]
        new_line = f"{indent}from {new_parent} import {new_name}"
        if new_name != bound_name:
            new_line += f" as {bound_name}"
        if remaining:
            names = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in remaining)
            replacement = f"{indent}from {parent} import {names}\n"
            new_line += "\n"
        else:
            replacement = ""
            new_line += f"{trailing_comment}\n"

        before = "".join(lines[: target.lineno - 1])
        after = "".join(lines[target.end_lineno :])
        path.write_text(before + replacement + new_line + after)
        touched.append(str(path))
        diffs.append((str(path), old_stmt, (replacement + new_line).rstrip("\n")))

    if not touched:
        print(f"no whole-module imports of {name!r} from {parent!r} found")
        sys.exit(0)

    subprocess.run(["uv", "run", "ruff", "check", "--fix", "--unsafe-fixes", "--select", "I,F401", *touched])
    subprocess.run(["uv", "run", "ruff", "format", *touched], check=True)
    print(f"rewired {name} ({parent} -> {new_parent}.{new_name}) in {len(touched)} file(s):")
    for path_str, old_stmt, new_stmt in diffs:
        print(f"  {path_str}")
        for line in old_stmt.splitlines():
            print(f"    - {line}")
        for line in new_stmt.splitlines():
            print(f"    + {line}")
