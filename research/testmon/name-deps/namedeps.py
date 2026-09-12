"""Prototype: name-level module dependency analyzer.

Splits each first-party module's top-level statements into blocks, resolves
name references across modules (including relative imports, package
re-exports, and `import *`), folds "effect" statements into the bindings of
the first-party objects they mutate, and computes dependency closures either
from an explicit set of seed references or (approximately) from "everything a
whole file references" for precision measurement against a real repo.

See the accompanying report for the design rationale and known gaps. Every
`# GAP:` comment marks a construct that is punted on.
"""

from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

STOPWORDS = {
    "get", "set", "id", "name", "type", "data", "value", "values", "index",
    "key", "keys", "list", "str", "int", "bool", "float", "dict", "self",
    "cls", "args", "kwargs", "none", "true", "false", "path", "url", "item",
    "items", "result", "response", "request", "config", "default", "message",
    "status", "code", "error", "test", "main", "run", "start", "end", "new",
}

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

MUTATING_CALL_NAMES = {"globals", "vars", "exec", "eval"}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockKey:
    module: str
    index: int

    def __repr__(self) -> str:
        return f"{self.module}#{self.index}"


@dataclass(frozen=True)
class WholeModule:
    module: str

    def __repr__(self) -> str:
        return f"{self.module}#*"


DepItem = BlockKey | WholeModule


@dataclass
class RefSet:
    names: set[str] = field(default_factory=set)          # bare Name loads
    attrs: set[tuple[str, ...]] = field(default_factory=set)  # dotted chains rooted at a Name
    strings: set[str] = field(default_factory=set)         # raw string literals seen
    bare_module_names: set[str] = field(default_factory=set)  # Names used bare that are import aliases


@dataclass
class Block:
    module: str
    index: int
    node: ast.stmt
    kind: str
    binds: set[str] = field(default_factory=set)
    refs: RefSet = field(default_factory=RefSet)
    is_effect: bool = False
    effect_refs: RefSet = field(default_factory=RefSet)  # narrower set used for effect-folding
    body_refs: RefSet | None = None  # full-body refs, only set for def blocks;
    # this is what a *call* to the function pulls in (distinct from `refs`,
    # which is what *defining* it needs -- signature/decorators only).
    import_aliases: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    # name -> (source_module, source_name_or_None). source_name None means
    # "name is bound to the *module object* source_module".

    def lineno(self) -> int:
        return self.node.lineno


@dataclass
class Module:
    name: str
    path: Path
    blocks: list[Block] = field(default_factory=list)
    # name -> indices of blocks binding it (module-local only)
    bindings: dict[str, list[int]] = field(default_factory=dict)
    whole_module_fallback: bool = False
    fallback_reasons: set[str] = field(default_factory=set)
    global_effects: list[int] = field(default_factory=list)  # block indices
    is_package: bool = False
    dunder_all: list[str] | None = None


# --------------------------------------------------------------------------
# Reference extraction
# --------------------------------------------------------------------------


def _dotted_chain(node: ast.expr) -> tuple[str, ...] | None:
    """If `node` is `a.b.c...` rooted at a bare Name, return ('a','b','c')."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return tuple(parts)
    return None


def _try_parse_forward_ref(s: str) -> ast.expr | None:
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError:
        return None
    return tree.body


def _walk_refs(node: ast.AST, out: RefSet, *, head_only: bool, in_annotation: bool = False) -> None:
    """Collect references from `node`.

    head_only=True stops descending into nested FunctionDef/AsyncFunctionDef
    bodies (only their decorators/signature are consumed) -- this is the mode
    used to build a top-level block's own reference set. head_only=False does
    a full walk (used for "what does this function's body reference" / "what
    does this whole test file reference").
    """
    if isinstance(node, ast.Attribute):
        chain = _dotted_chain(node)
        if chain is not None:
            out.attrs.add(chain)
            return
        _walk_refs(node.value, out, head_only=head_only)
        return

    if isinstance(node, ast.Name):
        if isinstance(node.ctx, ast.Load):
            out.names.add(node.id)
        return

    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            s = node.value
            out.strings.add(s)
            if in_annotation:
                parsed = _try_parse_forward_ref(s)
                if parsed is not None:
                    _walk_refs(parsed, out, head_only=head_only)
        return

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for d in node.decorator_list:
            _walk_refs(d, out, head_only=head_only)
        _walk_refs(node.args, out, head_only=head_only, in_annotation=True)
        if node.returns is not None:
            _walk_refs(node.returns, out, head_only=head_only, in_annotation=True)
        for tp in getattr(node, "type_params", []):
            _walk_refs(tp, out, head_only=head_only)
        if head_only:
            return  # skip node.body
        for stmt in node.body:
            _walk_refs(stmt, out, head_only=head_only)
        return

    if isinstance(node, ast.arg):
        if node.annotation is not None:
            _walk_refs(node.annotation, out, head_only=head_only, in_annotation=True)
        return

    if isinstance(node, ast.ClassDef):
        for d in node.decorator_list:
            _walk_refs(d, out, head_only=head_only)
        for b in node.bases:
            _walk_refs(b, out, head_only=head_only)
        for kw in node.keywords:
            _walk_refs(kw.value, out, head_only=head_only)
        for tp in getattr(node, "type_params", []):
            _walk_refs(tp, out, head_only=head_only)
        for stmt in node.body:
            # head_only mode still recurses into class-body *statements*
            # (fields etc); only nested def/class bodies get the head-only
            # treatment, applied recursively by this same function.
            _walk_refs(stmt, out, head_only=head_only)
        return

    if isinstance(node, ast.AnnAssign):
        _walk_refs(node.target, out, head_only=head_only)
        _walk_refs(node.annotation, out, head_only=head_only, in_annotation=True)
        if node.value is not None:
            _walk_refs(node.value, out, head_only=head_only)
        return

    for child in ast.iter_child_nodes(node):
        _walk_refs(child, out, head_only=head_only, in_annotation=in_annotation)


def extract_head_refs(stmt: ast.stmt) -> RefSet:
    out = RefSet()
    _walk_refs(stmt, out, head_only=True)
    return out


def extract_full_refs(node: ast.AST) -> RefSet:
    out = RefSet()
    _walk_refs(node, out, head_only=False)
    return out


# --------------------------------------------------------------------------
# Binding collection (recurses into If/Try/For/While/With, not into def/class)
# --------------------------------------------------------------------------


def _target_names(target: ast.expr) -> set[str]:
    names: set[str] = set()
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            names |= _target_names(elt)
    elif isinstance(target, ast.Starred):
        names |= _target_names(target.value)
    # Attribute / Subscript targets bind nothing new (they mutate) -- effect.
    return names


def _is_effect_target(target: ast.expr) -> bool:
    """True if assigning to `target` mutates an existing object rather than
    binding a fresh name (attribute/subscript targets)."""
    if isinstance(target, (ast.Attribute, ast.Subscript)):
        return True
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_is_effect_target(e) for e in target.elts)
    if isinstance(target, ast.Starred):
        return _is_effect_target(target.value)
    return False


def collect_bound_names(stmt: ast.stmt) -> set[str]:
    """Names this statement (recursively through If/Try/For/While/With, not
    through nested def/class bodies) binds at the level it appears."""
    names: set[str] = set()

    def visit(s: ast.stmt) -> None:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(s.name)
            return  # don't descend into body
        if isinstance(s, ast.Assign):
            for t in s.targets:
                names.update(_target_names(t))
        elif isinstance(s, ast.AnnAssign):
            names.update(_target_names(s.target))
        elif isinstance(s, ast.AugAssign):
            names.update(_target_names(s.target))
        elif isinstance(s, getattr(ast, "TypeAlias", ())):
            names.add(s.name.id)
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            pass  # handled separately (import_aliases)
        elif isinstance(s, (ast.If,)):
            for sub in s.body + s.orelse:
                visit(sub)
        elif isinstance(s, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            for sub in s.body:
                visit(sub)
            for h in s.handlers:
                if h.name:
                    names.add(h.name)
                for sub in h.body:
                    visit(sub)
            for sub in s.orelse + s.finalbody:
                visit(sub)
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            names.update(_target_names(s.target))
            for sub in s.body + s.orelse:
                visit(sub)
        elif isinstance(s, ast.While):
            for sub in s.body + s.orelse:
                visit(sub)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for item in s.items:
                if item.optional_vars is not None:
                    names.update(_target_names(item.optional_vars))
            for sub in s.body:
                visit(sub)
        # Expr, Delete, Assert, Return(invalid at module level), Global,
        # Nonlocal, Pass, Break, Continue, Raise: bind nothing.

    visit(stmt)
    return names


def statement_has_mutating_call(stmt: ast.stmt) -> bool:
    """True if globals()/vars()/exec()/eval() is called directly in this
    top-level statement (not inside a nested def body)."""
    found = False

    def visit(n: ast.AST, top: bool) -> None:
        nonlocal found
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return  # nested function body doesn't make the *module* untrusted
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in MUTATING_CALL_NAMES:
            found = True
            return
        for child in ast.iter_child_nodes(n):
            visit(child, top=False)

    visit(stmt, top=True)
    return found


def is_effect_statement(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Expr):
        return True
    if isinstance(stmt, ast.Delete):
        return True
    if isinstance(stmt, ast.Assign):
        return any(_is_effect_target(t) for t in stmt.targets)
    if isinstance(stmt, ast.AugAssign):
        return _is_effect_target(stmt.target)
    if isinstance(stmt, ast.AnnAssign):
        return _is_effect_target(stmt.target)
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return bool(stmt.decorator_list)
    if isinstance(stmt, (ast.If, ast.Try, getattr(ast, "TryStar", ast.Try), ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
        # effect if any directly-nested simple statement is itself an effect
        for sub in ast.walk(stmt):
            if sub is stmt:
                continue
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if sub.decorator_list:
                    return True
                continue
            if isinstance(sub, ast.stmt) and is_effect_statement(sub):
                return True
        return False
    return False


def effect_ref_source(stmt: ast.stmt) -> RefSet:
    """Narrow reference set used to decide what an effect statement mutates."""
    out = RefSet()
    if isinstance(stmt, ast.Expr):
        _walk_refs(stmt.value, out, head_only=True)
    elif isinstance(stmt, ast.Delete):
        for t in stmt.targets:
            _walk_refs(t, out, head_only=True)
    elif isinstance(stmt, ast.Assign):
        for t in stmt.targets:
            if _is_effect_target(t):
                _walk_refs(t, out, head_only=True)
        _walk_refs(stmt.value, out, head_only=True)
    elif isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
        _walk_refs(stmt.target, out, head_only=True)
        if stmt.value is not None:
            _walk_refs(stmt.value, out, head_only=True)
    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        for d in stmt.decorator_list:
            _walk_refs(d, out, head_only=True)
    else:
        # compound statement folded as an effect: gather refs of its nested
        # effect statements only (decorators / mutating targets), not the
        # whole subtree, to keep the fold reasonably narrow.
        for sub in ast.walk(stmt):
            if sub is stmt or not isinstance(sub, ast.stmt):
                continue
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if sub.decorator_list:
                    for d in sub.decorator_list:
                        _walk_refs(d, out, head_only=True)
                continue
            if is_effect_statement(sub):
                sub_out = effect_ref_source(sub)
                out.names |= sub_out.names
                out.attrs |= sub_out.attrs
                out.strings |= sub_out.strings
    return out


def factory_closure_mutation_targets(funcdef: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Heuristic for the decorator-factory pattern
    `def register(name):\n    def decorator(fn): REGISTRY[name] = fn; return fn\n    return decorator`.

    Finds base names of Subscript/Attribute *store* targets, anywhere in
    `funcdef`'s body (including nested defs), that aren't local to the
    factory -- i.e. names the factory's closures mutate on an enclosing
    scope. One level of interprocedural look-through, not a general points-to
    analysis: it will not follow a second hop (a factory that calls another
    helper to do the mutation)."""
    local_names: set[str] = set()
    for a in (funcdef.args.args, funcdef.args.posonlyargs, funcdef.args.kwonlyargs):
        local_names.update(x.arg for x in a)
    targets: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not funcdef:
            for a in (node.args.args, node.args.posonlyargs, node.args.kwonlyargs):
                local_names.update(x.arg for x in a)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
                    if t.value.id not in local_names:
                        targets.add(t.value.id)
                elif isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    if t.value.id not in local_names:
                        targets.add(t.value.id)
                elif isinstance(t, ast.Name):
                    local_names.add(t.id)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in funcdef.body:
        visit(stmt)
    return targets


def statement_kind(stmt: ast.stmt) -> str:
    return type(stmt).__name__


# --------------------------------------------------------------------------
# Import alias extraction
# --------------------------------------------------------------------------


def module_exists(modules: dict[str, Module], name: str) -> bool:
    return name in modules


def resolve_relative(modules: dict[str, Module], current: Module, level: int, module: str | None) -> str | None:
    pkg_parts = current.name.split(".")
    if current.is_package:
        base_parts = pkg_parts  # a package's "own package" is itself
    else:
        base_parts = pkg_parts[:-1]
    if level > 1:
        base_parts = base_parts[: -(level - 1)] if level - 1 <= len(base_parts) else []
    base = ".".join(base_parts)
    if module:
        return f"{base}.{module}" if base else module
    return base or None


def build_import_aliases(modules: dict[str, Module], mod: Module, stmt: ast.stmt) -> dict[str, tuple[str, str | None]]:
    aliases: dict[str, tuple[str, str | None]] = {}
    if isinstance(stmt, ast.Import):
        for alias in stmt.names:
            dotted = alias.name
            bound = alias.asname or dotted.split(".")[0]
            if alias.asname:
                # "import a.b.c as x" binds x -> module a.b.c directly
                aliases[bound] = (dotted, None)
            else:
                # "import a.b.c" binds a -> module 'a' (walking deeper
                # happens via dotted-chain resolution against `modules`)
                top = dotted.split(".")[0]
                aliases[top] = (top, None)
    elif isinstance(stmt, ast.ImportFrom):
        if stmt.level and stmt.level > 0:
            src = resolve_relative(modules, mod, stmt.level, stmt.module)
        else:
            src = stmt.module
        if src is None:
            return aliases
        for alias in stmt.names:
            if alias.name == "*":
                continue  # handled specially by caller
            bound = alias.asname or alias.name
            candidate_mod = f"{src}.{alias.name}"
            if module_exists(modules, candidate_mod):
                aliases[bound] = (candidate_mod, None)
            else:
                aliases[bound] = (src, alias.name)
    return aliases


def has_star_import(stmt: ast.stmt) -> str | None:
    if isinstance(stmt, ast.ImportFrom):
        for alias in stmt.names:
            if alias.name == "*":
                return stmt.module or ""
    return None


# --------------------------------------------------------------------------
# Module discovery / parsing
# --------------------------------------------------------------------------


def discover_modules(root: Path, package_name: str) -> dict[str, tuple[Path, bool]]:
    """Map dotted module name -> (path, is_package) for every .py file under
    `root` (root itself is the top package directory, e.g. .../fastapi)."""
    out: dict[str, tuple[Path, bool]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent)
        parts = list(rel.with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        dotted = ".".join(parts)
        out[dotted] = (path, is_package)
    return out


def parse_module(name: str, path: Path, is_package: bool, modules: dict[str, Module]) -> Module:
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        tree = ast.parse("", filename=str(path))
    mod = Module(name=name, path=path, is_package=is_package)
    star_targets: list[tuple[int, str]] = []

    for idx, stmt in enumerate(tree.body):
        kind = statement_kind(stmt)
        binds = collect_bound_names(stmt)
        head = extract_head_refs(stmt)
        effect = is_effect_statement(stmt)
        import_aliases: dict[str, tuple[str, str | None]] = {}

        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            import_aliases = build_import_aliases(modules, mod, stmt)
            binds |= set(import_aliases)
            star_mod = has_star_import(stmt)
            if star_mod is not None:
                if stmt.level and stmt.level > 0:
                    resolved = resolve_relative(modules, mod, stmt.level, stmt.module)
                else:
                    resolved = star_mod
                star_targets.append((idx, resolved or ""))

        block = Block(
            module=name,
            index=idx,
            node=stmt,
            kind=kind,
            binds=binds,
            refs=head,
            is_effect=effect,
            effect_refs=effect_ref_source(stmt) if effect else RefSet(),
            import_aliases=import_aliases,
            body_refs=extract_full_refs(stmt) if kind in ("FunctionDef", "AsyncFunctionDef") else None,
        )
        mod.blocks.append(block)
        for n in binds:
            mod.bindings.setdefault(n, []).append(idx)

        if kind == "FunctionDef" and stmt.name == "__getattr__":
            mod.whole_module_fallback = True
            mod.fallback_reasons.add("module __getattr__ (PEP 562)")
        if statement_has_mutating_call(stmt):
            mod.whole_module_fallback = True
            mod.fallback_reasons.add("globals()/vars()/exec()/eval() at module level")
        if binds == {"__all__"} and isinstance(stmt, ast.Assign):
            val = stmt.value
            if isinstance(val, (ast.List, ast.Tuple)):
                names = [e.value for e in val.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                mod.dunder_all = names

    mod._star_targets = star_targets  # type: ignore[attr-defined]
    return mod


# --------------------------------------------------------------------------
# Analyzer: ties modules together, does resolution + closure
# --------------------------------------------------------------------------


class Analyzer:
    def __init__(self) -> None:
        self.modules: dict[str, Module] = {}
        self._resolve_cache: dict[tuple[str, str], frozenset[DepItem]] = {}
        self._resolving: set[tuple[str, str]] = set()
        self.gaps: list[str] = []
        self._effects_by_target_block: dict[BlockKey, list[BlockKey]] = {}
        self._effects_by_target_module: dict[str, list[BlockKey]] = {}
        self._last_seg_index: dict[str, list[tuple[str, str]]] | None = None

    # -- construction ----------------------------------------------------

    def add_tree(self, root: Path, package_name: str | None = None) -> None:
        found = discover_modules(root, package_name or root.name)
        # two-phase: need `modules` dict populated with (name -> Module) as we
        # parse, but import-alias building needs to know which dotted names
        # are modules. Do a placeholder pass first.
        for dotted, (path, is_pkg) in found.items():
            self.modules[dotted] = Module(name=dotted, path=path, is_package=is_pkg)
        for dotted, (path, is_pkg) in found.items():
            self.modules[dotted] = parse_module(dotted, path, is_pkg, self.modules)

    def finalize(self) -> None:
        self._apply_star_imports()
        self._build_effect_index()

    def _apply_star_imports(self) -> None:
        for mod in self.modules.values():
            for idx, target in getattr(mod, "_star_targets", []):
                if target not in self.modules:
                    self.gaps.append(
                        f"{mod.name}: `from {target or '.'} import *` targets a "
                        "non-first-party or unresolved module; star names not bound "
                        "(GAP: any names actually re-exported from it are invisible)."
                    )
                    continue
                src = self.modules[target]
                public = src.dunder_all if src.dunder_all is not None else [
                    n for n in src.bindings if not n.startswith("_")
                ]
                block = mod.blocks[idx]
                for n in public:
                    if n in block.import_aliases:
                        continue
                    candidate_mod = f"{target}.{n}"
                    if candidate_mod in self.modules:
                        block.import_aliases[n] = (candidate_mod, None)
                    else:
                        block.import_aliases[n] = (target, n)
                    block.binds.add(n)
                    mod.bindings.setdefault(n, []).append(idx)

    def _build_effect_index(self) -> None:
        for mod in self.modules.values():
            for block in mod.blocks:
                if not block.is_effect:
                    continue
                targets = self._resolve_refset(mod.name, block.effect_refs, for_effect_fold=True)
                fp_targets = list(targets)
                fp_targets.extend(self._decorator_factory_widened_targets(mod, block))
                if not fp_targets:
                    mod.global_effects.append(block.index)
                    continue
                key = BlockKey(mod.name, block.index)
                for t in fp_targets:
                    if isinstance(t, WholeModule):
                        self._effects_by_target_module.setdefault(t.module, []).append(key)
                    else:
                        self._effects_by_target_block.setdefault(t, []).append(key)

    def _decorator_factory_widened_targets(self, mod: Module, block: Block) -> list[DepItem]:
        """One-hop interprocedural widening: if this block is a decorated
        def/class whose decorator is a call to a first-party decorator
        *factory*, and that factory's body mutates some enclosing-scope
        name via subscript/attribute (the `@register("x") def handler` /
        `REGISTRY[name] = fn` pattern), fold this effect into whatever that
        mutated name resolves to as well -- not just into the factory
        function's own definition."""
        node = block.node
        decorators: list[ast.expr] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            decorators = node.decorator_list
        else:
            for sub in ast.walk(node):
                if sub is node:
                    continue
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and sub.decorator_list:
                    decorators.extend(sub.decorator_list)
        out: list[DepItem] = []
        for dec in decorators:
            if not isinstance(dec, ast.Call):
                continue
            func_refs = RefSet()
            _walk_refs(dec.func, func_refs, head_only=True)
            factory_targets = self._resolve_refset(mod.name, func_refs, for_effect_fold=True)
            for t in factory_targets:
                if not isinstance(t, BlockKey):
                    continue
                factory_block = self.modules[t.module].blocks[t.index]
                if not isinstance(factory_block.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                mutated = factory_closure_mutation_targets(factory_block.node)
                for name in mutated:
                    out.extend(self.resolve_binding(t.module, name))
        return out

    # -- resolution --------------------------------------------------------

    def _walk_module_chain(self, start_module: str, chain: tuple[str, ...]) -> DepItem | None:
        """Walk a.b.c... starting from a known first-party `start_module`
        bound as the chain's root, descending into submodules while they
        exist, then resolving the first non-module attribute as a name in
        whatever module we've reached."""
        cur_mod = start_module
        for i, step in enumerate(chain):
            candidate = f"{cur_mod}.{step}"
            if candidate in self.modules:
                cur_mod = candidate
                continue
            # `step` is a top-level name inside cur_mod
            if cur_mod not in self.modules:
                return None
            return ("name", cur_mod, step)  # type: ignore[return-value]
        # chain fully consumed as nested modules -> bare module reference
        return WholeModule(cur_mod) if False else ("module", cur_mod)  # type: ignore[return-value]

    def resolve_binding(self, module_name: str, name: str) -> frozenset[DepItem]:
        cache_key = (module_name, name)
        if cache_key in self._resolve_cache:
            return self._resolve_cache[cache_key]
        if cache_key in self._resolving:
            return frozenset()  # cycle guard
        self._resolving.add(cache_key)
        try:
            result = self._resolve_binding_uncached(module_name, name)
        finally:
            self._resolving.discard(cache_key)
        self._resolve_cache[cache_key] = result
        return result

    def _resolve_binding_uncached(self, module_name: str, name: str) -> frozenset[DepItem]:
        mod = self.modules.get(module_name)
        if mod is None:
            return frozenset()
        if mod.whole_module_fallback:
            return frozenset({WholeModule(module_name)})
        return self._resolve_binding_core(module_name, name)

    def resolve_binding_ignoring_fallback(self, module_name: str, name: str) -> frozenset[DepItem]:
        """Like `resolve_binding`, but used when expanding a WholeModule item
        that's already in the closure: chases what `name` binds to (through
        re-exports) even though the module is itself marked whole-module
        fallback, since "depend on all of it" must still mean something more
        than "depend on its own literal statements" for a re-export hub."""
        if self.modules.get(module_name) is None:
            return frozenset()
        cache_key = ("!ignoring_fallback", module_name, name)
        if cache_key in self._resolve_cache:
            return self._resolve_cache[cache_key]
        if cache_key in self._resolving:
            return frozenset()
        self._resolving.add(cache_key)
        try:
            result = self._resolve_binding_core(module_name, name)
        finally:
            self._resolving.discard(cache_key)
        self._resolve_cache[cache_key] = result
        return result

    def _resolve_binding_core(self, module_name: str, name: str) -> frozenset[DepItem]:
        mod = self.modules[module_name]
        out: set[DepItem] = set()
        for idx in mod.bindings.get(name, []):
            block = mod.blocks[idx]
            if name in block.import_aliases:
                src_mod, src_name = block.import_aliases[name]
                if src_name is None:
                    if src_mod in self.modules:
                        out.add(WholeModule(src_mod))
                    # else: third-party module alias, not tracked
                else:
                    if src_mod in self.modules:
                        out |= self.resolve_binding(src_mod, src_name)
                    # else: name imported from third-party/stdlib module
            else:
                out.add(BlockKey(module_name, idx))
        return frozenset(out)

    def _resolve_refset(self, home_module: str, refs: RefSet, *, for_effect_fold: bool = False) -> set[DepItem]:
        """Shallow (non-transitive) resolution of a RefSet's entries into
        DepItems, used both for effect-fold target discovery and as a seed
        step for closures."""
        out: set[DepItem] = set()
        mod = self.modules.get(home_module)
        if mod is None:
            return out
        if mod.whole_module_fallback:
            return {WholeModule(home_module)}

        for n in refs.names:
            if n in mod.bindings:
                out |= self.resolve_binding(home_module, n)
                # bare module reference check
                for idx in mod.bindings[n]:
                    b = mod.blocks[idx]
                    if n in b.import_aliases and b.import_aliases[n][1] is None:
                        src_mod = b.import_aliases[n][0]
                        if src_mod in self.modules and src_mod != home_module:
                            self.modules[src_mod].whole_module_fallback = True
                            self.modules[src_mod].fallback_reasons.add(
                                f"module object referenced bare as `{n}` in {home_module}"
                            )
                            out.add(WholeModule(src_mod))

        for chain in refs.attrs:
            base, *rest = chain
            if base not in mod.bindings:
                continue
            for idx in mod.bindings[base]:
                b = mod.blocks[idx]
                if base not in b.import_aliases:
                    # base is a first-party name bound locally, not a module
                    # alias -- attribute access on it isn't a module hop; the
                    # dependency is just on `base` itself.
                    out |= self.resolve_binding(home_module, base)
                    continue
                src_mod, src_name = b.import_aliases[base]
                if src_name is None:
                    start = src_mod
                else:
                    # from X import Y as base, Y not itself a submodule:
                    # resolve Y in X, then stop the chain (one hop).
                    out |= self.resolve_binding(src_mod, src_name)
                    continue
                if start not in self.modules:
                    continue
                walked = self._walk_module_chain(start, tuple(rest)) if rest else ("module", start)
                if walked is None:
                    continue
                if walked[0] == "module":
                    out.add(WholeModule(walked[1]))
                else:
                    _, wmod, wname = walked
                    out |= self.resolve_binding(wmod, wname)

        if not for_effect_fold:
            for s in refs.strings:
                if not IDENT_RE.match(s):
                    continue
                seg = s.rsplit(".", 1)[-1]
                if seg in STOPWORDS or len(seg) < 3:
                    continue
                for tgt_mod, tgt_name in self._last_segment_index().get(seg, []):
                    out |= self.resolve_binding(tgt_mod, tgt_name)
        return out

    def _last_segment_index(self) -> dict[str, list[tuple[str, str]]]:
        if self._last_seg_index is None:
            idx: dict[str, list[tuple[str, str]]] = {}
            for mod in self.modules.values():
                for n in mod.bindings:
                    idx.setdefault(n, []).append((mod.name, n))
            self._last_seg_index = idx
        return self._last_seg_index

    # -- closure -----------------------------------------------------------

    def closure(self, home_module: str, refs: RefSet) -> set[DepItem]:
        """Full transitive dependency closure for a set of seed references
        made from `home_module` (a def-block's body, or a whole file)."""
        result: set[DepItem] = set()
        touched_modules: set[str] = set()
        queue: list[DepItem] = list(self._resolve_refset(home_module, refs))
        effect_queue: list[BlockKey] = []

        def touch(m: str) -> None:
            if m in touched_modules:
                return
            touched_modules.add(m)
            mod = self.modules.get(m)
            if mod is None:
                return
            for idx in mod.global_effects:
                effect_queue.append(BlockKey(m, idx))
            for key in self._effects_by_target_module.get(m, []):
                effect_queue.append(key)

        while queue or effect_queue:
            while queue:
                item = queue.pop()
                if item in result:
                    continue
                result.add(item)
                if isinstance(item, WholeModule):
                    touch(item.module)
                    # A whole-module fallback means "everyone depending on
                    # anything in this module depends on all of it" -- which,
                    # for a re-export hub, includes chasing each of its own
                    # bindings (import aliases included) to their real
                    # targets, not just this module's own literal blocks.
                    wmod = self.modules.get(item.module)
                    if wmod is not None:
                        for name in wmod.bindings:
                            queue.extend(self.resolve_binding_ignoring_fallback(item.module, name))
                    continue
                touch(item.module)
                block = self.modules[item.module].blocks[item.index]
                continuation = block.body_refs if block.body_refs is not None else block.refs
                queue.extend(self._resolve_refset(item.module, continuation))
                for extra in self._effects_by_target_block.get(item, []):
                    effect_queue.append(extra)
            while effect_queue:
                key = effect_queue.pop()
                if key in result:
                    continue
                result.add(key)
                touch(key.module)
                block = self.modules[key.module].blocks[key.index]
                continuation = block.body_refs if block.body_refs is not None else block.refs
                queue.extend(self._resolve_refset(key.module, continuation))
        return result

    def expand(self, closure_result: set[DepItem]) -> set[BlockKey]:
        out: set[BlockKey] = set()
        for item in closure_result:
            if isinstance(item, BlockKey):
                out.add(item)
            else:
                mod = self.modules[item.module]
                out.update(BlockKey(item.module, i) for i in range(len(mod.blocks)))
        return out

    # -- convenience ---------------------------------------------------

    def file_refs(self, module_name: str) -> RefSet:
        """Approximate 'everything this file (as a test file) references':
        union of full-walk refs over every top-level statement."""
        mod = self.modules[module_name]
        out = RefSet()
        for block in mod.blocks:
            full = extract_full_refs(block.node)
            out.names |= full.names
            out.attrs |= full.attrs
            out.strings |= full.strings
        return out

    def static_import_closure_blocks(self, module_name: str, seen: set[str] | None = None) -> set[str]:
        """The file-level rule's dependency set: every first-party module
        reachable via static imports from `module_name` (transitively)."""
        seen = seen if seen is not None else set()
        if module_name in seen or module_name not in self.modules:
            return seen
        seen.add(module_name)
        mod = self.modules[module_name]
        for block in mod.blocks:
            for src_mod, _ in block.import_aliases.values():
                if src_mod in self.modules:
                    self.static_import_closure_blocks(src_mod, seen)
        return seen


def build_analyzer(*roots: tuple[Path, str]) -> Analyzer:
    a = Analyzer()
    for root, pkg in roots:
        a.add_tree(root, pkg)
    a.finalize()
    return a


if __name__ == "__main__":
    import sys

    t0 = time.time()
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("proj/pkg")
    a = build_analyzer((root, root.name))
    dt = time.time() - t0
    n_blocks = sum(len(m.blocks) for m in a.modules.values())
    print(f"parsed {len(a.modules)} modules, {n_blocks} blocks in {dt:.3f}s")
    print("fallback modules:", {m.name: m.fallback_reasons for m in a.modules.values() if m.whole_module_fallback})
