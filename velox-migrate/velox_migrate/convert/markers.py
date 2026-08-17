"""The `VELOX-TODO` comments converted source carries, and where each one lands.

A marker is the tool's half of the promise that nothing changes meaning quietly: every construct
the conversion did not translate, and every one it translated with a caveat, is named in the
source a reviewer reads. The comment carries the support-matrix code, which is what a report
section, a finding and a rewrite rule already reconcile against, so grepping one code finds every
view of the same decision.

A marker attaches to the function it concerns rather than to the line the construct sits on:
rewrites move lines, and a qualified name survives them. The exact line is the report's to quote.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

import libcst as cst

from velox_migrate import matrix

PREFIX = "VELOX-TODO"

# Every marker this could emit, for the existence check that makes a second run change nothing.
_EXISTING = re.compile(rf"{PREFIX}\[(VX\d+)\]")


def comment(code: str) -> str:
    """The one-line marker for a support-matrix code, as it is written into source.

    What a reader needs at the site is the decision and what to do about it; the reasoning behind
    it belongs in the report, which the code leads back to. So the text is the row's action where
    it has one, and otherwise the construct it names.
    """
    construct = matrix.construct(code)
    return f"# {PREFIX}[{code}]: {_one_line(construct.action or construct.subject)}"


def mark(module: cst.Module, wanted: Mapping[str, Iterable[str]]) -> cst.Module:
    """`module` with a marker above each function `wanted` names, keyed by qualified name.

    The empty qualname marks the module itself, and its comments land after the docstring, where a
    reader meets them before any test. A code already marked on a target is left as it is, so
    converting an already-converted tree writes nothing.
    """
    codes = {qualname: tuple(sorted(set(found))) for qualname, found in wanted.items() if found}
    if not codes:
        return module
    return module.visit(_Marker(codes))


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _already_marked(lines: Iterable[cst.EmptyLine]) -> set[str]:
    found: set[str] = set()
    for line in lines:
        if line.comment is not None:
            found |= set(_EXISTING.findall(line.comment.value))
    return found


def _lines(codes: Iterable[str]) -> list[cst.EmptyLine]:
    """One comment line per code, indented as whatever block it lands in already is."""
    return [cst.EmptyLine(indent=True, comment=cst.Comment(comment(code))) for code in codes]


class _Marker(cst.CSTTransformer):
    """Attaches markers to the definitions named in `codes`, tracking qualified names as it goes."""

    def __init__(self, codes: Mapping[str, tuple[str, ...]]) -> None:
        super().__init__()
        self._codes = codes
        self._path: list[str] = []

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        self._path.append(node.name.value)
        return True

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        self._path.append(node.name.value)
        return True

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        self._path.pop()
        return self._attach(updated_node)

    def leave_ClassDef(
        self, original_node: cst.ClassDef, updated_node: cst.ClassDef
    ) -> cst.ClassDef:
        self._path.pop()
        return self._attach(updated_node)

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        wanted = self._codes.get("")
        if not wanted:
            return updated_node
        body = list(updated_node.body)
        position = 1 if body and _is_docstring(body[0]) else 0
        if position >= len(body):
            return updated_node
        target = body[position]
        if not isinstance(target, cst.SimpleStatementLine | cst.BaseCompoundStatement):
            return updated_node
        leading = list(target.leading_lines)
        missing = [code for code in wanted if code not in _already_marked(leading)]
        if not missing:
            return updated_node
        body[position] = target.with_changes(leading_lines=[*leading, *_lines(missing)])
        return updated_node.with_changes(body=body)

    def _attach[T: cst.FunctionDef | cst.ClassDef](self, node: T) -> T:
        qualname = ".".join([*self._path, node.name.value])
        wanted = self._codes.get(qualname)
        if not wanted:
            return node
        leading = list(node.leading_lines)
        missing = [code for code in wanted if code not in _already_marked(leading)]
        if not missing:
            return node
        return node.with_changes(leading_lines=[*leading, *_lines(missing)])


def _is_docstring(statement: cst.BaseStatement) -> bool:
    return (
        isinstance(statement, cst.SimpleStatementLine)
        and len(statement.body) == 1
        and isinstance(statement.body[0], cst.Expr)
        and isinstance(statement.body[0].value, cst.SimpleString)
    )
