"""Splits one file's source into statement and def blocks (see `plans/affected-tests-plan.md`,
Fingerprints), from a single `ast.parse`.

A **statement block** covers one module top-level statement -- for a bare `def`, just its name
and decorators (the part that runs at import); for a `class`, its bases, keywords, decorators,
class-level statements, and each method's name and decorators, but not method bodies. A **def
block** covers one function or method's signature, type params and body, however deeply nested,
since every function or method gets its own `co_qualname` regardless of depth -- unlike a class,
whose *body* code object (`class bodies` in `tracer`'s "Mapping a code object to a block") has no
qualname of its own and is instead attributed to whichever block's source span contains it. That
is why only a *literal module top-level* class gets a dedicated block: a nested class's body runs
inline in the code object of whatever contains it (another function's body, or another class's),
so its content -- bases, decorators, non-method statements -- stays embedded there too. Its
methods still get their own def blocks regardless, since those are separate code objects at any
depth.

A nested `def`'s name and decorators stay embedded in whichever block contains it, the same way;
only a literal module top-level `def` gets an *additional* standalone statement block for its own
name and decorators.

Each block's checksum is `blake2b-8` of `ast.dump(include_attributes=False)` over its own content,
salted with its identifying key (a def block's qualname, or a statement block's sorted bound
names) -- comment- and whitespace-insensitive by construction, and cheap at scale (see the
Fingerprints probe report, `research/affected/reports/fingerprint-probes.md`).

Resolving these into dependency keys -- the name closure, effect folding, string index, and
whole-module fallback -- is `resolve.py`, not built yet. `binds`/`references`/`effect` are
therefore unused so far outside this module's own tests.
"""

from __future__ import annotations

import ast
import copy
import hashlib
from dataclasses import dataclass

__all__ = ["Block", "parse_blocks"]


@dataclass(frozen=True, slots=True)
class Block:
    """One statement or def block. `qualname` is set for a def block (`Outer.Inner.method`,
    matching `co_qualname`) and `None` for a statement block, which is instead identified by the
    names it binds -- empty for a statement that only has an effect, e.g. `app.include_router(r)`.

    `references` and `effect` are meaningful for statement blocks (rule 2's name closure and rule
    3's effect folding); a def block's `binds` is always empty; its `references` still matters for
    the same name closure, over its own signature and body.
    """

    qualname: str | None
    first_line: int
    binds: frozenset[str]
    references: frozenset[str]
    effect: bool
    checksum: bytes


def parse_blocks(source: str, filename: str = "<unknown>") -> list[Block]:
    """One parse of `source`, split into the blocks described in this module's docstring."""
    tree = ast.parse(source, filename=filename)
    blocks: list[Block] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stub = _carve_def(stmt, (), blocks)
            blocks.append(_build_statement_block(stub))
        elif isinstance(stmt, ast.ClassDef):
            _carve_class(stmt, blocks)
        else:
            blocks.append(_build_statement_block(_carve_nested(stmt, (), blocks)))
    return blocks


# Carving -- rebuilding each container's own AST with every nested def/class pulled out into its
# own block(s) and left as a stub or, for an embedded class, folded in with only its own methods
# pulled out. Only ever produces the additional pieces this module's docstring describes; never
# resolves a reference or classifies an effect across files -- that is `resolve.py`.
# ------------------------------------------------------------------------


def _carve_body(
    stmts: list[ast.stmt], scope: tuple[str, ...], blocks: list[Block]
) -> list[ast.stmt]:
    """Rebuilds a body -- a def's, a class's, or a control-flow statement's -- carving every
    nested def into its own block and folding every nested class in place (see module
    docstring)."""
    residual: list[ast.stmt] = []
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            residual.append(_carve_def(stmt, scope, blocks))
        elif isinstance(stmt, ast.ClassDef):
            residual.append(_embed_class(stmt, scope, blocks))
        else:
            residual.append(_carve_nested(stmt, scope, blocks))
    return residual


def _carve_nested(stmt: ast.stmt, scope: tuple[str, ...], blocks: list[Block]) -> ast.stmt:
    """Rebuilds `stmt`'s own statement-list fields (`if`/`try`/`for`/`while`/`with`/`match`),
    carving any def/class nested inside however deep. Anything else -- an assignment, an import,
    a bare expression -- has no statement-list field and passes through unchanged: a def or class
    can only ever appear inside one of these, never inside an expression."""
    if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While)):
        rebuilt = copy.copy(stmt)
        rebuilt.body = _carve_body(stmt.body, scope, blocks)
        rebuilt.orelse = _carve_body(stmt.orelse, scope, blocks)
        return rebuilt
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        rebuilt = copy.copy(stmt)
        rebuilt.body = _carve_body(stmt.body, scope, blocks)
        return rebuilt
    if isinstance(stmt, (ast.Try, ast.TryStar)):
        rebuilt = copy.copy(stmt)
        rebuilt.body = _carve_body(stmt.body, scope, blocks)
        rebuilt.orelse = _carve_body(stmt.orelse, scope, blocks)
        rebuilt.finalbody = _carve_body(stmt.finalbody, scope, blocks)
        rebuilt.handlers = [_carve_handler(h, scope, blocks) for h in stmt.handlers]
        return rebuilt
    if isinstance(stmt, ast.Match):
        rebuilt = copy.copy(stmt)
        rebuilt.cases = [_carve_case(c, scope, blocks) for c in stmt.cases]
        return rebuilt
    return stmt


def _carve_handler(
    handler: ast.ExceptHandler, scope: tuple[str, ...], blocks: list[Block]
) -> ast.ExceptHandler:
    rebuilt = copy.copy(handler)
    rebuilt.body = _carve_body(handler.body, scope, blocks)
    return rebuilt


def _carve_case(
    case: ast.match_case, scope: tuple[str, ...], blocks: list[Block]
) -> ast.match_case:
    rebuilt = copy.copy(case)
    rebuilt.body = _carve_body(case.body, scope, blocks)
    return rebuilt


def _carve_def(
    funcdef: ast.FunctionDef | ast.AsyncFunctionDef, scope: tuple[str, ...], blocks: list[Block]
) -> ast.stmt:
    """Appends `funcdef`'s own def block -- signature, type params and body, decorators excluded
    since those belong to whichever block embeds the returned stub -- and returns that stub:
    `funcdef` itself with its signature and body emptied, carrying only its name and decorators."""
    qualname = ".".join((*scope, funcdef.name))
    inner_scope = (*scope, funcdef.name, "<locals>")
    body_node = copy.copy(funcdef)
    body_node.decorator_list = []
    body_node.body = _carve_body(funcdef.body, inner_scope, blocks)
    blocks.append(
        Block(
            qualname=qualname,
            first_line=_first_line(funcdef),
            binds=frozenset(),
            references=_collect_references(body_node),
            effect=False,
            checksum=_checksum(body_node, qualname),
        )
    )
    stub = copy.copy(funcdef)
    stub.args = ast.arguments(
        posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]
    )
    stub.returns = None
    stub.type_params = []
    stub.body = [ast.Pass()]
    return stub


def _embed_class(
    classdef: ast.ClassDef, scope: tuple[str, ...], blocks: list[Block]
) -> ast.ClassDef:
    """A class that is not a literal module top-level statement: its own class-body code object
    has no dedicated source span to be mapped onto (see module docstring), so its bases,
    decorators and non-method statements stay embedded here; only its methods, and any further
    nested def, are carved out, since each of those is a distinct code object regardless of
    depth."""
    embedded = copy.copy(classdef)
    embedded.body = _carve_body(classdef.body, (*scope, classdef.name), blocks)
    return embedded


def _carve_class(classdef: ast.ClassDef, blocks: list[Block]) -> None:
    """Only for a literal module top-level `class` statement: appends its one statement block --
    bases, keywords, decorators, class-level statements, and each method's name and decorators
    (method bodies are carved into their own def blocks by `_carve_body`)."""
    content = copy.copy(classdef)
    content.body = _carve_body(classdef.body, (classdef.name,), blocks)
    blocks.append(_build_statement_block(content))


# Per-block metadata -- binds, references, effect, checksum
# ------------------------------------------------------------------------


def _build_statement_block(node: ast.stmt) -> Block:
    binds = _bound_names(node)
    return Block(
        qualname=None,
        first_line=_first_line(node),
        binds=binds,
        references=_collect_references(node),
        effect=_is_effect(node),
        checksum=_checksum(node, ",".join(sorted(binds))),
    )


def _first_line(node: ast.stmt) -> int:
    """The line a code object mapped to this block would report as `co_firstlineno`: the first
    decorator's line for a decorated def or class, its own `def`/`class` line otherwise."""
    decorators: list[ast.expr] | None = getattr(node, "decorator_list", None)
    if decorators:
        return decorators[0].lineno
    return node.lineno


def _collect_references(node: ast.AST) -> frozenset[str]:
    """Every name `node` reads -- an attribute chain `a.b.c` is covered by its root `Name` node
    alone, without walking the chain, since `a` is exactly what a reference to it needs to
    resolve. String literals (rule 5) and dotted-import resolution (rule 7) are `resolve.py`'s,
    not collected here."""
    return frozenset(
        n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    )


def _checksum(node: ast.AST, salt: str) -> bytes:
    digest = hashlib.blake2b(digest_size=8)
    digest.update(salt.encode())
    digest.update(b"\0")
    digest.update(ast.dump(node, include_attributes=False).encode())
    return digest.digest()


# Binds and effects -- which names a statement binds, and whether it mutates instead
# ------------------------------------------------------------------------


def _target_names(target: ast.expr) -> frozenset[str]:
    """Names an assignment target binds -- empty for an `Attribute`/`Subscript` target, which
    mutates an existing object rather than binding a fresh name (see `_is_effect_target`)."""
    if isinstance(target, ast.Name):
        return frozenset((target.id,))
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for elt in target.elts:
            names |= _target_names(elt)
        return frozenset(names)
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return frozenset()


def _is_effect_target(target: ast.expr) -> bool:
    if isinstance(target, (ast.Attribute, ast.Subscript)):
        return True
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_is_effect_target(elt) for elt in target.elts)
    if isinstance(target, ast.Starred):
        return _is_effect_target(target.value)
    return False


def _bound_names(stmt: ast.stmt) -> frozenset[str]:
    """Names `stmt` binds at its own level, recursing through `if`/`try`/`for`/`while`/`with`/
    `match` -- but not into a nested def or class, which by now is either a stub or an embedded
    node (see `_carve_body`) and contributes only its own name."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return frozenset((stmt.name,))
    if isinstance(stmt, ast.Assign):
        return frozenset().union(*(_target_names(t) for t in stmt.targets))
    if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        return _target_names(stmt.target)
    if isinstance(stmt, ast.TypeAlias):
        return frozenset((stmt.name.id,))
    if isinstance(stmt, ast.Import):
        return frozenset((alias.asname or alias.name).split(".")[0] for alias in stmt.names)
    if isinstance(stmt, ast.ImportFrom):
        return frozenset(alias.asname or alias.name for alias in stmt.names if alias.name != "*")
    return _bound_names_compound(stmt)


def _bound_names_body(stmts: list[ast.stmt]) -> frozenset[str]:
    names: set[str] = set()
    for stmt in stmts:
        names.update(_bound_names(stmt))
    return frozenset(names)


def _bound_names_compound(stmt: ast.stmt) -> frozenset[str]:
    """`_bound_names`'s control-flow half: `if`/`try`/`for`/`while`/`with`/`match`. Everything
    else -- `Expr`, `Delete`, `Assert`, `Return`, `Global`, `Nonlocal`, `Pass`, `Break`,
    `Continue`, `Raise` -- binds nothing."""
    if isinstance(stmt, ast.If):
        return _bound_names_body(stmt.body) | _bound_names_body(stmt.orelse)
    if isinstance(stmt, (ast.Try, ast.TryStar)):
        return _bound_names_try(stmt)
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return (
            _target_names(stmt.target)
            | _bound_names_body(stmt.body)
            | _bound_names_body(stmt.orelse)
        )
    if isinstance(stmt, ast.While):
        return _bound_names_body(stmt.body) | _bound_names_body(stmt.orelse)
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        names = {
            name
            for item in stmt.items
            if item.optional_vars is not None
            for name in _target_names(item.optional_vars)
        }
        return frozenset(names) | _bound_names_body(stmt.body)
    if isinstance(stmt, ast.Match):
        names = set()
        for case in stmt.cases:
            names.update(_pattern_names(case.pattern))
            names.update(_bound_names_body(case.body))
        return frozenset(names)
    return frozenset()


def _bound_names_try(stmt: ast.Try | ast.TryStar) -> frozenset[str]:
    names = set(_bound_names_body(stmt.body))
    for handler in stmt.handlers:
        if handler.name:
            names.add(handler.name)
        names.update(_bound_names_body(handler.body))
    names.update(_bound_names_body(stmt.orelse))
    names.update(_bound_names_body(stmt.finalbody))
    return frozenset(names)


def _pattern_names(pattern: ast.pattern) -> frozenset[str]:
    """Names a `match` pattern captures: `case x:`/`case [*rest]` (`MatchAs`/`MatchStar`) and
    `case {**rest}` (`MatchMapping.rest`)."""
    names: set[str] = set()
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
    return frozenset(names)


_COMPOUND_STMTS = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.TryStar,
    ast.Match,
)


def _is_effect(stmt: ast.stmt) -> bool:
    """Whether `stmt` mutates something it references rather than only binding a fresh name --
    rule 3's effect statements, before folding onto a target, which is `resolve.py`'s job."""
    if isinstance(stmt, (ast.Expr, ast.Delete)):
        return True
    if isinstance(stmt, ast.Assign):
        return any(_is_effect_target(target) for target in stmt.targets)
    if isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
        return _is_effect_target(stmt.target)
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return bool(stmt.decorator_list)
    if isinstance(stmt, ast.ClassDef):
        # Unlike a def's body -- its own, separate def block -- a class's (carved) body is part
        # of this same statement block, so a decorated method or an effect statement among its
        # class-level statements makes the class itself an effect too.
        return bool(stmt.decorator_list) or _is_effect_compound(stmt)
    if isinstance(stmt, _COMPOUND_STMTS):
        return _is_effect_compound(stmt)
    return False


def _is_effect_compound(stmt: ast.stmt) -> bool:
    """True if any statement nested in `stmt` -- however deep through further control flow -- is
    itself an effect. A nested def/class only counts through its own decorators: its (already
    carved-out or embedded) body is not this statement's own."""
    for sub in ast.walk(stmt):
        if sub is stmt:
            continue
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if sub.decorator_list:
                return True
            continue
        if isinstance(sub, ast.stmt) and _is_effect(sub):
            return True
    return False
