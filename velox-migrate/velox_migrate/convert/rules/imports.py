"""The import rule: a leading dot resolved against the module the suite actually holds.

velox imports test modules under synthetic `velox_tests.*` names and takes them back out of
`sys.modules` afterwards, so a relative import inside a suite has nothing to resolve against —
neither while the module is being read nor later, from inside a body. The absolute spelling of the
same module is what a suite says instead, and it is the module's own path that says what that is.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import libcst as cst

from velox_migrate.convert.rules import Rule, RuleTransformer, rule

__all__ = ["RULES"]


class _Relative(RuleTransformer):
    """VX034: `from .sibling import name` as the absolute import of the same module."""

    CODE = "VX034"

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.ImportFrom:
        absolute = self._absolute(updated_node)
        if absolute is None:
            return updated_node
        self.record(
            f"`{_spelled(updated_node)}` is relative, and velox imports this module under a "
            f"synthetic name; it becomes `from {absolute} import ...`.",
            qualname=None,
        )
        return updated_node.with_changes(relative=[], module=_attribute(absolute))

    def _absolute(self, node: cst.ImportFrom) -> str | None:
        """The dotted module `node`'s dots point at, or `None` if this is not a relative import.

        `None` too where the dots reach past the root the suite was collected from: a package
        above the suite is one nothing here can name, and leaving the import alone keeps the
        failure the suite already had rather than inventing a module. A file two packages deep
        (`parts` two long) resolves a level-1 or level-2 import -- pytest's own
        `_resolve_name(name, package, level)` needs `len(package.split('.')) >= level`, and
        `parts` is `package.split('.')` here -- but not level-3, which is the same "beyond
        top-level package" `ImportError` pytest itself raises.
        """
        level = len(node.relative)
        if level == 0:
            return None
        parts = PurePosixPath(self.context.path).parent.parts
        if level > len(parts):
            return None
        held = parts[: len(parts) - (level - 1)]
        tail = _dotted(node.module) if node.module is not None else ""
        joined = ".".join([*held, *([tail] if tail else [])])
        return joined or None


def _dotted(node: cst.BaseExpression) -> str:
    """`a.b.c` as written, for the module half of an `ImportFrom`."""
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        return f"{_dotted(node.value)}.{node.attr.value}"
    return ""


def _attribute(dotted: str) -> cst.BaseExpression:
    """`tests.helpers` as the node an `ImportFrom` holds its module in."""
    node: cst.BaseExpression = cst.Name(dotted.split(".")[0])
    for part in dotted.split(".")[1:]:
        node = cst.Attribute(value=node, attr=cst.Name(part))
    return node


def _spelled(node: cst.ImportFrom) -> str:
    """The import as it was written, for the line the rule files."""
    return f"from {'.' * len(node.relative)}{_dotted(node.module) if node.module else ''} import"


RULES: tuple[Rule, ...] = (rule(_Relative),)
