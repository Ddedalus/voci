"""The numbered rewrite rules, and the vocabulary every one of them is written in.

One rule per support-matrix row: it matches a single pytest source form, writes the voci spelling
of it, and files an `Applied` line naming what it did — or, where the form is too tangled to
rewrite, naming the row that declines it and leaving the source alone. Every rule matches only a
form its own rewrite removes, so running the whole set twice leaves a module byte-identical to
running it once, which is what lets `convert` be re-run over a tree it has already converted in
part.

Two invariants hold across the set. A rule reads `pytest` spellings through
`QualifiedNameProvider`, so `import pytest as pt` and `from pytest import mark` are the same mark
to all of them. And a rule writes no import: it emits `voci.`-qualified names, assuming
`import voci`, and any other module a rewrite needs travels in the `Applied` record for the
caller to act on.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, final

import libcst as cst
from libcst.metadata import MetadataWrapper, ParentNodeProvider, QualifiedNameProvider

from voci_migrate.convert.parametrize import Generated

__all__ = [
    "RULES",
    "Applied",
    "Context",
    "Result",
    "Rule",
    "RuleTransformer",
    "TransformerRule",
    "apply_all",
    "enabled",
    "rule",
]


@dataclass(frozen=True, slots=True)
class Context:
    """What a rule knows about the file it is rewriting.

    `axis_ids` is keyed by a test's qualname and its `argnames` joined by commas, and holds
    pytest's own ids for that one parametrize axis, in case order. `tests` are the qualnames the
    dump collected as tests in this file, a method spelled `TestGroup.test_name`; a mark means
    nothing on anything else, so a mark rule touches a function only if it is named there.
    `blocked` are the qualnames whose source stays verbatim, refused functions and everything
    downstream of a refusal, and no rule writes inside one or on its decorators.

    `requested` and `finalizers` carry what the plan decided about the bodies in this file, keyed
    by qualname: which name a `request.getfixturevalue` becomes the parameter of, and whose
    `request.addfinalizer` calls become the teardown after a `yield`. A rule writes only where
    they say so — the shapes that decide it are the audit's to read, not a rewrite's.

    `indirect` and `generated` carry the same answer about the cases a call site decided: which
    `indirect` marks the fixture they name is about to carry as `params=`, as the argnames each
    covers, and which axes a `pytest_generate_tests` hook produced become a parametrize of their
    own, outermost first.
    """

    path: str
    axis_ids: Mapping[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    tests: frozenset[str] = frozenset()
    blocked: frozenset[str] = frozenset()
    xfail_strict: bool = False
    requested: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    finalizers: frozenset[str] = frozenset()
    indirect: Mapping[str, frozenset[str]] = field(default_factory=dict)
    generated: Mapping[str, Sequence[Generated]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Applied:
    """One thing a rule did, or declined to do, at one site.

    `code` is the support-matrix row the line reconciles against — the rule's own row for a
    rewrite, and the row that refuses the construct where the source was left alone. `needs` names
    the modules the rewritten source reads and the original did not, which the caller imports: a
    rule writes no import of its own, and pytest evaluated a string `skipif` condition with `sys`,
    `os` and `platform` already in scope where the module may never have imported them.
    """

    code: str
    qualname: str | None
    message: str
    needs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Result:
    """A module after one rule, with that rule's lines."""

    module: cst.Module
    applied: tuple[Applied, ...]


class Rule(Protocol):
    """One numbered rewrite, `code` being the support-matrix row it implements."""

    @property
    def code(self) -> str: ...

    def apply(self, module: cst.Module, context: Context) -> Result: ...


@dataclass(slots=True)
class _Frame:
    """One enclosing `def` or `class`, with what a rule needs while it is inside.

    `params` is empty for a class, whose body binds nothing a nested function sees. It is what
    tells a fixture object from a local of the same name: a `capsys` no enclosing `def` takes as
    a parameter is not pytest's.
    """

    qualname: str
    params: frozenset[str] = frozenset()
    is_function: bool = False


class RuleTransformer(cst.CSTTransformer):
    """One rule's pass over one module, tracking the enclosing qualname as it goes.

    `CODE` is the support-matrix row the subclass implements and the row `record` files under.
    Subclasses guard every rewrite with `is_blocked` and, for a mark, `is_test`: those two are
    what keep a refused function verbatim and keep a rule off a function that is not collected.

    Metadata is resolved over the module as handed in, so `get_metadata` — and the `names` and
    `parent` helpers over it — may only be asked about a node that came from there, never about
    one a rewrite built.
    """

    METADATA_DEPENDENCIES = (ParentNodeProvider, QualifiedNameProvider)

    CODE: str = ""

    def __init__(self, context: Context) -> None:
        super().__init__()
        self.context = context
        self.records: list[Applied] = []
        self._stack: list[_Frame] = [_Frame(qualname="")]

    # --- scopes ----------------------------------------------------------------------------

    def on_visit(self, node: cst.CSTNode) -> bool:
        if isinstance(node, cst.FunctionDef | cst.ClassDef):
            name = node.name.value
            enclosing = self._stack[-1].qualname
            self._stack.append(
                _Frame(
                    qualname=f"{enclosing}.{name}" if enclosing else name,
                    params=(
                        _param_names(node.params)
                        if isinstance(node, cst.FunctionDef)
                        else frozenset()
                    ),
                    is_function=isinstance(node, cst.FunctionDef),
                )
            )
        return super().on_visit(node)

    def on_leave(
        self, original_node: cst.CSTNodeT, updated_node: cst.CSTNodeT
    ) -> cst.CSTNodeT | cst.RemovalSentinel | cst.FlattenSentinel[cst.CSTNodeT]:
        # Popped after the subclass's own `leave_*` has run, so a rule reading `qualname` while
        # leaving a `def` sees that `def`, not the scope around it.
        result = super().on_leave(original_node, updated_node)
        if isinstance(original_node, cst.FunctionDef | cst.ClassDef):
            self._stack.pop()
        return result

    def abandon(self, original_node: cst.CSTNode) -> None:
        """Pop the frame `on_visit` pushed for `original_node`, without running `leave_*`.

        Used by `_Pipeline` when an earlier pass in the same traversal has already removed or
        flattened this node: there is nothing left here for this pass to rewrite, but it still
        owes the stack the pop its own `on_leave` would have made.
        """
        if isinstance(original_node, cst.FunctionDef | cst.ClassDef):
            self._stack.pop()

    @property
    def qualname(self) -> str | None:
        """The `def` or `class` being visited, dotted, or `None` at module level."""
        return self._stack[-1].qualname or None

    @property
    def is_blocked(self) -> bool:
        """Whether this site sits in something `Context.blocked` names."""
        return any(frame.qualname in self.context.blocked for frame in self._stack[1:])

    @property
    def at_module_level(self) -> bool:
        """Whether the `def` being left is written at module level rather than inside anything."""
        return len(self._stack) == 2

    @property
    def is_test(self) -> bool:
        """Whether the enclosing `def` is one the dump collected as a test."""
        return self._stack[-1].qualname in self.context.tests

    def takes(self, name: str) -> bool:
        """Whether an enclosing `def` takes `name` as a parameter, so it is a fixture object."""
        return any(frame.is_function and name in frame.params for frame in self._stack)

    # --- metadata --------------------------------------------------------------------------

    def names(self, node: cst.CSTNode) -> frozenset[str]:
        """The qualified names `node` resolves to, falling back to how it is spelled.

        The fallback is what recognizes a call whose module the file never imports, which happens
        in a conftest that reaches its dependency through a star import.
        """
        resolved = {name.name for name in self.get_metadata(QualifiedNameProvider, node, ())}
        if resolved:
            return frozenset(resolved)
        spelled = dotted(node.func if isinstance(node, cst.Call) else node)
        return frozenset({spelled}) if spelled else frozenset()

    def parent(self, node: cst.CSTNode) -> cst.CSTNode | None:
        """The node `node` hangs off, as the module was handed in."""
        return self.get_metadata(ParentNodeProvider, node, None)

    def record(
        self,
        message: str,
        *,
        code: str | None = None,
        qualname: str | None = None,
        needs: tuple[str, ...] = (),
    ) -> None:
        """File one `Applied`, coded to this rule and sited at the enclosing `def` by default."""
        self.records.append(
            Applied(
                code=code or self.CODE,
                qualname=qualname if qualname is not None else self.qualname,
                message=message,
                needs=needs,
            )
        )


@final
@dataclass(frozen=True, slots=True)
class TransformerRule:
    """A rule that is one `RuleTransformer` over the module."""

    code: str
    transformer: type[RuleTransformer]

    def apply(self, module: cst.Module, context: Context) -> Result:
        """`module` rewritten by this rule alone, with the lines the pass filed."""
        # `unsafe_skip_copy` keeps node identity between the tree metadata was resolved over and
        # the tree being visited, which is what lets a pass recognize a node it saw in a
        # pre-scan. Safe here because a transformer builds new nodes rather than mutating.
        wrapper = MetadataWrapper(module, unsafe_skip_copy=True)
        pass_ = self.transformer(context)
        return Result(module=wrapper.visit(pass_), applied=tuple(pass_.records))


def rule(transformer: type[RuleTransformer]) -> TransformerRule:
    """`transformer` as a rule, coded by its own `CODE`."""
    return TransformerRule(code=transformer.CODE, transformer=transformer)


def apply_all(
    module: cst.Module, active: Sequence[Rule], context: Context
) -> tuple[cst.Module, list[Applied]]:
    """`module` rewritten by every rule in `active`, in order, over one metadata resolution.

    Equivalent to folding `Rule.apply` over `active`, but `ParentNodeProvider` and
    `QualifiedNameProvider` are resolved once against `module` rather than once per rule: every
    rule in `RULES` is a `TransformerRule`, so its pass can share one `MetadataWrapper`'s metadata
    instead of each building its own and paying for a fresh scope resolution. Raises `TypeError`
    for a `Rule` that isn't one, since there is nothing to share a pass with.
    """
    passes: list[RuleTransformer] = []
    for transformer_rule in active:
        if not isinstance(transformer_rule, TransformerRule):
            raise TypeError(
                f"apply_all only supports TransformerRule rules, got {transformer_rule.code!r}"
            )
        passes.append(transformer_rule.transformer(context))
    wrapper = MetadataWrapper(module, unsafe_skip_copy=True)
    shared = wrapper.resolve_many((ParentNodeProvider, QualifiedNameProvider))
    for pass_ in passes:
        pass_.metadata = shared
    new_module = wrapper.module.visit(_Pipeline(passes))
    assert isinstance(new_module, cst.Module)
    return new_module, [applied for pass_ in passes for applied in pass_.records]


class _Pipeline(cst.CSTTransformer):
    """Every pass in `passes`, run as one traversal in the order they're given.

    Each node visits through every pass in turn — the `updated_node` one pass leaves behind is
    what the next pass sees — which is what makes this equivalent to running each pass's own
    `wrapper.visit()` in sequence. No current rule removes or flattens a node; if one ever does,
    the passes after it are told to `abandon` that node rather than fed the sentinel in its
    place, since only `RuleTransformer`'s own frame-stack bookkeeping still needs to run there.
    """

    def __init__(self, passes: Sequence[RuleTransformer]) -> None:
        super().__init__()
        self._passes = passes

    def on_visit(self, node: cst.CSTNode) -> bool:
        visit_children = False
        for pass_ in self._passes:
            if pass_.on_visit(node):
                visit_children = True
        return visit_children

    def on_leave(
        self, original_node: cst.CSTNodeT, updated_node: cst.CSTNodeT
    ) -> cst.CSTNodeT | cst.RemovalSentinel | cst.FlattenSentinel[cst.CSTNodeT]:
        for index, pass_ in enumerate(self._passes):
            result = pass_.on_leave(original_node, updated_node)
            if not isinstance(result, cst.CSTNode):
                # Nothing is left here for a later pass to rewrite, but each one still pushed a
                # frame for this node in `on_visit` and owes the stack the pop its own `on_leave`
                # would have made.
                for remaining in self._passes[index + 1 :]:
                    remaining.abandon(original_node)
                return result
            updated_node = result
        return updated_node


# --- expression helpers, shared by the mark and body rules ----------------------------------


def voci(name: str) -> cst.Attribute:
    """`voci.<name>`, the spelling every rule emits."""
    return cst.Attribute(value=cst.Name("voci"), attr=cst.Name(name))


def string(text: str) -> cst.SimpleString:
    """`text` as a double-quoted Python string literal.

    Written through `json.dumps`, whose escaping of quotes, backslashes and control characters is
    read the same way by Python, so an id or a reason carrying any of them survives verbatim.
    """
    return cst.SimpleString(json.dumps(text, ensure_ascii=False))


def dotted(node: cst.CSTNode) -> str | None:
    """`a.b.c` for a chain of plain names, and `None` for anything else."""
    parts: list[str] = []
    while isinstance(node, cst.Attribute):
        parts.append(node.attr.value)
        node = node.value
    if not isinstance(node, cst.Name):
        return None
    parts.append(node.value)
    return ".".join(reversed(parts))


def positional(call: cst.Call) -> tuple[cst.Arg, ...]:
    """A call's plain positional arguments, `*args` excluded."""
    return tuple(arg for arg in call.args if arg.keyword is None and arg.star == "")


def keyword(call: cst.Call, name: str) -> cst.Arg | None:
    """The `name=` argument of `call`, if it has one."""
    for arg in call.args:
        if arg.keyword is not None and arg.keyword.value == name:
            return arg
    return None


def keywords(call: cst.Call) -> frozenset[str]:
    """Every keyword `call` passes by name."""
    return frozenset(arg.keyword.value for arg in call.args if arg.keyword is not None)


def starred(call: cst.Call) -> bool:
    """Whether `call` unpacks anything, which is what makes its arguments uncountable."""
    return any(arg.star in ("*", "**") for arg in call.args)


def double_starred(call: cst.Call) -> bool:
    """Whether `call` unpacks a mapping with `**`, which can carry a keyword `keywords` can't see
    since the mapping's contents aren't known until the call runs.
    """
    return any(arg.star == "**" for arg in call.args)


def argument(value: cst.BaseExpression, name: str | None = None) -> cst.Arg:
    """One argument, positional or `name=`, spelled without spaces around the `=`."""
    if name is None:
        return cst.Arg(value=value)
    return cst.Arg(
        value=value,
        keyword=cst.Name(name),
        equal=cst.AssignEqual(cst.SimpleWhitespace(""), cst.SimpleWhitespace("")),
    )


def called(func: cst.BaseExpression, args: Sequence[cst.Arg], like: cst.Call | None) -> cst.Call:
    """A call to `func` with `args`, laid out the way `like` was.

    The last argument inherits `like`'s trailing whitespace and comma, so a call written over
    several lines stays over several lines and one written on one line stays on one line.
    """
    if like is None or not like.args or not args:
        return cst.Call(func=func, args=list(args))
    last = like.args[-1]
    fixed = list(args)
    fixed[-1] = fixed[-1].with_changes(
        whitespace_after_arg=last.whitespace_after_arg, comma=last.comma
    )
    return like.with_changes(func=func, args=fixed)


def with_keyword(call: cst.Call, name: str, value: cst.BaseExpression) -> cst.Call:
    """`call` with `name=value` replacing or following its own arguments, layout preserved.

    An appended argument takes the separator the call's own arguments are already separated by,
    so `name=` lands on its own line in a call written one argument per line.
    """
    existing = keyword(call, name)
    if existing is not None:
        return call.with_changes(
            args=[arg.with_changes(value=value) if arg is existing else arg for arg in call.args]
        )
    args = list(call.args)
    if not args:
        return call.with_changes(args=[argument(value, name)])
    separator = args[0].comma if isinstance(args[0].comma, cst.Comma) else cst.MaybeSentinel.DEFAULT
    tail = args[-1]
    args[-1] = tail.with_changes(comma=separator)
    args.append(
        argument(value, name).with_changes(
            comma=tail.comma, whitespace_after_arg=tail.whitespace_after_arg
        )
    )
    return call.with_changes(args=args)


def leading(statement: cst.BaseStatement) -> Sequence[cst.EmptyLine]:
    """The blank and comment lines written above `statement`."""
    if isinstance(statement, cst.SimpleStatementLine | cst.BaseCompoundStatement):
        return statement.leading_lines
    return ()


def render(node: cst.CSTNode) -> str:
    """`node` as one line of source, shortened to keep a message readable."""
    return shorten(" ".join(cst.Module(body=()).code_for_node(node).split()))


def shorten(text: str, limit: int = 48) -> str:
    """`text`, elided in the middle of a word rather than wrapped into a message."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def literal_text(node: cst.BaseExpression) -> str | None:
    """The value of a plain string literal, and `None` for anything else."""
    if isinstance(node, cst.SimpleString | cst.ConcatenatedString):
        value = node.evaluated_value
        return value if isinstance(value, str) else None
    return None


def _param_names(params: cst.Parameters) -> frozenset[str]:
    names = {
        param.name.value
        for param in (*params.posonly_params, *params.params, *params.kwonly_params)
    }
    for star in (params.star_arg, params.star_kwarg):
        if isinstance(star, cst.Param):
            names.add(star.name.value)
    return frozenset(names)


def _collect() -> tuple[Rule, ...]:
    """Every rule, in code order.

    Imported here rather than at the top of the module: every rule module is written in this
    module's vocabulary, so they are loaded once it holds all of it.
    """
    from voci_migrate.convert.rules import bodies, cases, imports, marks

    return tuple(
        sorted(
            (*marks.RULES, *bodies.RULES, *cases.RULES, *imports.RULES),
            key=lambda rule: rule.code,
        )
    )


RULES: tuple[Rule, ...] = _collect()


def enabled(disabled: Collection[str]) -> tuple[Rule, ...]:
    """The rules to run, in code order, with every code named in `disabled` left out."""
    excluded = _codes(disabled)
    return tuple(rule for rule in RULES if rule.code not in excluded)


def _codes(codes: Iterable[str]) -> frozenset[str]:
    return frozenset(code.strip().upper() for code in codes)
