"""The mark rules: what a `pytest.mark` decorator, and a `pytestmark` assignment, become.

`_MarkPass.translate` is the one place that says which support-matrix row owns which mark shape,
and every rule here filters that answer by its own code — so a mark shape belongs to exactly one
rule, and the `pytestmark` distribution writes the same velox spelling a decorator would have.

velox reads marks from the test function and refuses one applied to a class, so a mark written for
a group of tests — a `pytestmark` assignment, or a decorator on a `class Test*` — is distributed
onto the tests themselves by `VX115`, and no other rule touches a class decorator.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import libcst as cst

from velox_migrate import matrix
from velox_migrate.convert.rules import (
    Context,
    RuleTransformer,
    TransformerRule,
    argument,
    called,
    dotted,
    keyword,
    keywords,
    leading,
    literal_text,
    positional,
    render,
    rule,
    starred,
    string,
    velox,
    with_keyword,
)

NO_REASON = "no reason given"

# The marks velox needs no spelling for: it runs `async def` tests itself.
_DROPPED = frozenset({"asyncio", "anyio"})

# The velox mark a rewrite sets at most once per test — applying one twice raises at import.
_SCALARS = frozenset({"skip", "xfail", "timeout"})

# Marks another area of the matrix owns, keyed to the row that answers for them.
_ELSEWHERE: dict[str, str] = {"filterwarnings": "VX108"}

# The names pytest puts in scope when it evaluates a string condition, beyond the module's own.
_CONDITION_NAMES = frozenset({"os", "sys", "platform"})


@dataclass(frozen=True, slots=True)
class _Mark:
    """One `pytest.mark.<name>`, with the call that carried its arguments where there was one."""

    name: str
    node: cst.BaseExpression
    call: cst.Call | None


@dataclass(frozen=True, slots=True)
class _Translated:
    """One mark, read.

    `owner` is the rule that writes this shape, and `None` for a mark another area of the matrix
    answers for — `usefixtures` is wiring's, a plugin's mark is nobody's. `code` is the row the
    outcome reconciles against: `owner`'s row for a rewrite, and the refusing row where the source
    is left alone. `expression` is the velox decorator, and `None` for a mark that goes away.
    `scalar` names the velox mark the rewrite sets, for the marks velox allows only once. `needs`
    names the modules the expression reads.
    """

    owner: str | None
    code: str
    message: str
    expression: cst.BaseExpression | None = None
    refused: bool = False
    scalar: str | None = None
    needs: tuple[str, ...] = ()


def _refuses(owner: str | None, code: str, message: str) -> _Translated:
    return _Translated(owner=owner, code=code, message=message, refused=True)


class _MarkPass(RuleTransformer):
    """Reading and writing one `pytest.mark.<name>`, shared by every rule in this module."""

    def mark_of(self, node: cst.BaseExpression) -> _Mark | None:
        """`node` as a mark, or `None` when it is not a `pytest.mark` spelling."""
        call = node if isinstance(node, cst.Call) else None
        target = call.func if call is not None else node
        for name in sorted(self.names(target)):
            prefix, _, mark = name.rpartition(".")
            if prefix == "pytest.mark" and mark:
                return _Mark(name=mark, node=node, call=call)
        return None

    def translate(
        self, mark: _Mark, *, qualname: str | None = None, strict_default: bool = False
    ) -> _Translated:
        """What `mark` becomes, and which rule writes it."""
        if mark.name == "parametrize":
            return self._parametrize(mark, qualname=qualname)
        if mark.name == "skip":
            return self._skip(mark)
        if mark.name == "skipif":
            return self._skipif(mark)
        if mark.name == "xfail":
            return self._xfail(mark, strict_default=strict_default)
        if mark.name == "timeout":
            return self._timeout(mark)
        if mark.name in _DROPPED:
            return _Translated(
                owner="VX110",
                code="VX110",
                message=f"`{render(mark.node)}` goes away: velox runs `async def` tests itself.",
            )
        if mark.name == "usefixtures":
            return _Translated(
                owner="VX009",
                code="VX009",
                message=(
                    f"`{render(mark.node)}` goes away: a `velox.use(...)` declaration on the "
                    "module gives the same tests the same fixtures."
                ),
            )
        if mark.name in matrix.PLUGIN_MARKS:
            distribution = matrix.PLUGIN_MARKS[mark.name]
            return _refuses(
                None,
                "VX112",
                f"`{render(mark.node)}` is a mark {distribution} acts on, and velox records marks "
                "as tags without acting on them.",
            )
        if mark.name in matrix.KNOWN_MARKS:
            return _refuses(
                None,
                _ELSEWHERE.get(mark.name, "VX115"),
                f"`{render(mark.node)}` is pytest's own mark, and no rule here writes it.",
            )
        return self._tag(mark)

    # --- one mark at a time --------------------------------------------------------------------

    def _skip(self, mark: _Mark) -> _Translated:
        call = mark.call
        reason: cst.BaseExpression | None = None
        if call is not None:
            if starred(call):
                return _refuses(
                    "VX104", "VX104", f"`{render(mark.node)}` unpacks its arguments at run time."
                )
            given = positional(call)
            named = keyword(call, "reason")
            if len(given) > 1 or keywords(call) - {"reason"}:
                return _refuses("VX104", "VX104", f"`{render(mark.node)}` is not `skip(reason)`.")
            if given:
                reason = given[0].value
            elif named is not None:
                reason = named.value
        expression = called(
            velox("skip"), [argument(reason if reason is not None else string(NO_REASON))], call
        )
        return _Translated(
            owner="VX104",
            code="VX104",
            scalar="skip",
            expression=expression,
            message=f"`{render(mark.node)}` becomes `{render(expression)}`.",
        )

    def _skipif(self, mark: _Mark) -> _Translated:
        call = mark.call
        if call is None or starred(call):
            return _refuses(
                "VX104", "VX104", f"`{render(mark.node)}` names no condition to skip on."
            )
        given = positional(call)
        named = keyword(call, "condition")
        condition = given[0].value if given else (named.value if named is not None else None)
        if condition is None:
            return _refuses(
                "VX104", "VX104", f"`{render(mark.node)}` names no condition to skip on."
            )
        reason_arg = keyword(call, "reason")
        reason = reason_arg.value if reason_arg is not None else None
        if reason is None and len(given) > 1:
            reason = given[1].value
        if len(given) > 2 or keywords(call) - {"condition", "reason"}:
            return _refuses(
                "VX104", "VX104", f"`{render(mark.node)}` is not `skipif(condition, reason=...)`."
            )
        text = literal_text(condition)
        needs: tuple[str, ...] = ()
        if text is not None:
            condition, needs, refusal = self._lazy_condition(mark, text)
            if refusal is not None:
                return refusal
        expression = called(
            velox("skipif"),
            [
                argument(condition),
                argument(reason if reason is not None else string(NO_REASON), "reason"),
            ],
            call,
        )
        if text is None:
            return _Translated(
                owner="VX104",
                code="VX104",
                expression=expression,
                message=f"`{render(mark.node)}` becomes `{render(expression)}`.",
            )
        wanted = "".join(f" It reads `{module}`." for module in needs)
        return _Translated(
            owner="VX103",
            code="VX103",
            expression=expression,
            needs=needs,
            message=(
                f"`{render(mark.node)}` becomes `{render(expression)}`, evaluated when the test "
                f"runs rather than while the module is imported.{wanted}"
            ),
        )

    def _lazy_condition(
        self, mark: _Mark, text: str
    ) -> tuple[cst.BaseExpression, tuple[str, ...], _Translated | None]:
        """A string condition as a zero-argument lambda, with the modules it reads."""
        try:
            condition = cst.parse_expression(text)
        except cst.ParserSyntaxError:
            return (
                mark.node,
                (),
                _refuses(
                    "VX103",
                    "VX103",
                    f"`{render(mark.node)}`'s condition is not one Python expression.",
                ),
            )
        reads = _root_names(condition)
        if "config" in reads:
            return (
                mark.node,
                (),
                _refuses(
                    "VX103",
                    "VX103",
                    f"`{render(mark.node)}`'s condition reads pytest's `config`, which velox has "
                    "no counterpart for.",
                ),
            )
        lazy = cst.Lambda(params=cst.Parameters(), body=condition)
        return lazy, tuple(sorted(reads & _CONDITION_NAMES)), None

    def _xfail(self, mark: _Mark, *, strict_default: bool) -> _Translated:
        call = mark.call
        if call is None:
            expression = cst.Call(
                func=velox("xfail"),
                args=[argument(string(NO_REASON)), *_strict(strict_default)],
            )
            return _Translated(
                owner="VX104",
                code="VX104",
                scalar="xfail",
                expression=expression,
                message=f"`{render(mark.node)}` becomes `{render(expression)}`.",
            )
        if starred(call):
            return _refuses(
                "VX104", "VX104", f"`{render(mark.node)}` unpacks its arguments at run time."
            )
        given = positional(call)
        named = keyword(call, "condition")
        if given or named is not None:
            return _refuses(
                "VX104",
                "VX105",
                f"`{render(mark.node)}` expects a failure only under a condition, and "
                "`@velox.xfail` applies unconditionally.",
            )
        if keywords(call) - {"reason", "strict", "raises", "run"}:
            return _refuses(
                "VX104", "VX104", f"`{render(mark.node)}` is not an `xfail` this rule reads."
            )
        run = keyword(call, "run")
        reason_arg = keyword(call, "reason")
        reason = reason_arg.value if reason_arg is not None else string(NO_REASON)
        if run is not None and not _is_bool(run.value):
            return _refuses(
                "VX106",
                "VX106",
                f"`{render(mark.node)}`'s `run=` is decided at run time, and whether the test "
                "runs at all is what has no counterpart.",
            )
        if run is not None and _is_false(run.value):
            expression = called(velox("skip"), [argument(reason)], call)
            dropped = keywords(call) & {"strict", "raises"}
            note = f" Its `{'`, `'.join(sorted(dropped))}` is dropped." if dropped else ""
            return _Translated(
                owner="VX106",
                code="VX106",
                scalar="skip",
                expression=expression,
                message=(
                    f"`{render(mark.node)}` becomes `{render(expression)}`: nothing in velox "
                    f"expects a failure without running the test.{note}"
                ),
            )
        args = [argument(reason)]
        strict = keyword(call, "strict")
        if strict is not None:
            args.append(argument(strict.value, "strict"))
        else:
            args.extend(_strict(strict_default))
        raises = keyword(call, "raises")
        if raises is not None:
            args.append(argument(raises.value, "raises"))
        expression = called(velox("xfail"), args, call)
        return _Translated(
            owner="VX104",
            code="VX104",
            scalar="xfail",
            expression=expression,
            message=f"`{render(mark.node)}` becomes `{render(expression)}`.",
        )

    def _timeout(self, mark: _Mark) -> _Translated:
        call = mark.call
        if call is None or starred(call):
            return _refuses("VX111", "VX111", f"`{render(mark.node)}` names no number of seconds.")
        given = positional(call)
        named = keyword(call, "timeout")
        seconds = given[0].value if given else (named.value if named is not None else None)
        if seconds is None or len(given) > 1:
            return _refuses("VX111", "VX111", f"`{render(mark.node)}` is not `timeout(seconds)`.")
        if _is_nonpositive(seconds):
            return _refuses(
                "VX111",
                "VX111",
                f"`{render(mark.node)}` is not a positive number of seconds, and "
                "`@velox.timeout` takes one.",
            )
        expression = called(velox("timeout"), [argument(seconds)], call)
        dropped = keywords(call) - {"timeout"}
        note = f" Its `{'`, `'.join(sorted(dropped))}` is dropped." if dropped else ""
        return _Translated(
            owner="VX111",
            code="VX111",
            scalar="timeout",
            expression=expression,
            message=f"`{render(mark.node)}` becomes `{render(expression)}`.{note}",
        )

    def _tag(self, mark: _Mark) -> _Translated:
        expression = cst.Call(func=velox("tag"), args=[argument(string(mark.name))])
        note = " Its arguments are dropped." if mark.call is not None and mark.call.args else ""
        return _Translated(
            owner="VX109",
            code="VX109",
            expression=expression,
            message=f"`{render(mark.node)}` becomes `{render(expression)}`.{note}",
        )

    # --- parametrize ---------------------------------------------------------------------------

    def _parametrize(self, mark: _Mark, *, qualname: str | None) -> _Translated:
        call = mark.call
        if call is None or starred(call):
            return _refuses(
                "VX101", "VX101", f"`{render(mark.node)}` names no argnames and argvalues."
            )
        given = positional(call)
        if len(given) != 2:
            return _refuses(
                "VX101",
                "VX101",
                f"`{render(mark.node)}` is not `parametrize(argnames, argvalues)`.",
            )
        if keyword(call, "indirect") is not None:
            return self._indirect(mark, _argnames(given[0].value), qualname=qualname)
        unknown = keywords(call) - {"ids"}
        if unknown:
            return _refuses(
                "VX101",
                "VX101",
                f"`{render(mark.node)}` passes `{'`, `'.join(sorted(unknown))}`, which "
                "`@velox.parametrize` has no counterpart for.",
            )

        ids_arg = keyword(call, "ids")
        names = _argnames(given[0].value)
        cases = self._unwrap(given[1].value, arity=len(names) if names else None)
        if isinstance(cases, _Translated):
            return cases
        values, case_ids = cases

        axis = None
        if names is not None and qualname is not None:
            axis = self.context.axis_ids.get((qualname, ",".join(names)))
        count = _case_count(values)
        ids: cst.BaseExpression | None = None
        note = ""
        if axis is not None and (count is None or count == len(axis)):
            ids = _tuple([string(text) for text in axis])
        elif ids_arg is not None:
            ids = ids_arg.value
        elif case_ids is not None:
            ids = _tuple(list(case_ids))
        else:
            note = (
                " Its case ids are composed from more than this one axis, so velox composes its "
                "own from the values."
            )

        expression = call
        if values is not given[1].value:
            expression = expression.with_changes(
                args=[
                    arg.with_changes(value=values) if arg is given[1] else arg
                    for arg in expression.args
                ]
            )
        expression = expression.with_changes(func=velox("parametrize"))
        if ids is not None:
            expression = with_keyword(expression, "ids", ids)
        return _Translated(
            owner="VX101",
            code="VX101" if not note else "VX114",
            expression=expression,
            message=f"`{render(mark.node)}` becomes `@velox.parametrize`.{note}",
        )

    def _indirect(
        self, mark: _Mark, names: tuple[str, ...] | None, *, qualname: str | None
    ) -> _Translated:
        """An `indirect` mark: gone where the fixture it names is about to carry its values.

        Whether it is depends on every test in the suite that reaches that fixture, which is the
        plan's answer and not a rule's — so all this reads is whether this axis is one the plan
        listed.
        """
        axis = ",".join(names) if names else None
        allowed = self.context.indirect.get(qualname or "", frozenset())
        if axis is not None and axis in allowed:
            return _Translated(
                owner="VX007",
                code="VX007",
                message=(
                    f"`{render(mark.node)}` goes away: `{axis}` carries these values as its own "
                    "`params=`."
                ),
            )
        return _refuses(
            "VX101",
            "VX029",
            f"`{render(mark.node)}` gives `{axis or 'a fixture'}` values a `params=` of its own "
            "cannot hold for every test that reaches it.",
        )

    def _unwrap(
        self, values: cst.BaseExpression, *, arity: int | None
    ) -> tuple[cst.BaseExpression, tuple[cst.BaseExpression, ...] | None] | _Translated:
        """`values` with every `pytest.param` replaced by the case it wrapped, and their ids.

        The ids come back only when every case carried one, since a partial list would label the
        wrong cases.
        """
        if not isinstance(values, cst.List | cst.Tuple):
            return values, None
        elements: list[cst.BaseElement] = []
        ids: list[cst.BaseExpression] = []
        rewritten = False
        for element in values.elements:
            wrapped = element.value
            if not (isinstance(wrapped, cst.Call) and "pytest.param" in self.names(wrapped)):
                elements.append(element)
                continue
            if starred(wrapped):
                return _refuses(
                    "VX101", "VX101", f"`{render(wrapped)}` unpacks its values at run time."
                )
            if keyword(wrapped, "marks") is not None:
                return _refuses(
                    "VX101",
                    "VX102",
                    f"`{render(wrapped)}` marks one case, and a velox mark applies to a whole "
                    "test.",
                )
            if keywords(wrapped) - {"id"}:
                return _refuses(
                    "VX101", "VX101", f"`{render(wrapped)}` is not `pytest.param(*values, id=...)`."
                )
            case = _case(positional(wrapped), arity=arity)
            if case is None:
                return _refuses(
                    "VX101",
                    "VX101",
                    f"`{render(wrapped)}` holds a different number of values than the argnames "
                    "name.",
                )
            given_id = keyword(wrapped, "id")
            if given_id is not None:
                ids.append(given_id.value)
            elements.append(element.with_changes(value=case))
            rewritten = True
        if not rewritten:
            return values, None
        complete = tuple(ids) if len(ids) == len(elements) else None
        return values.with_changes(elements=elements), complete

    # --- writing a decorator -------------------------------------------------------------------

    def rewrite(self, original: cst.Decorator, updated: cst.Decorator) -> cst.Decorator:
        """`updated` with its mark translated, when the mark's shape is this rule's to write."""
        if self.is_blocked or not self.is_test or not self.on_function(original):
            return updated
        mark = self.mark_of(original.decorator)
        if mark is None:
            return updated
        translated = self.translate(mark, qualname=self.qualname)
        if translated.owner != self.CODE:
            return updated
        if translated.refused or translated.expression is None:
            self.record(translated.message, code=translated.code)
            return updated
        if translated.scalar is not None and self._applied_twice(original, translated.scalar):
            self.record(
                f"`@velox.{translated.scalar}` would be applied twice to `{self.qualname}`, and "
                "velox raises rather than letting the last one win.",
                code="VX113",
            )
            return updated
        self.record(translated.message, code=translated.code, needs=translated.needs)
        return updated.with_changes(decorator=translated.expression)

    def on_function(self, decorator: cst.Decorator) -> bool:
        """Whether `decorator` decorates a `def`, rather than a class."""
        return isinstance(self.parent(decorator), cst.FunctionDef)

    def _applied_twice(self, decorator: cst.Decorator, scalar: str) -> bool:
        parent = self.parent(decorator)
        if not isinstance(parent, cst.FunctionDef):
            return False
        return sum(1 for other in parent.decorators if self.scalar_of(other) == scalar) > 1

    def scalar_of(self, decorator: cst.Decorator) -> str | None:
        """The velox mark this decorator sets, whichever of the two spellings it is written in."""
        node = decorator.decorator
        spelled = dotted(node.func if isinstance(node, cst.Call) else node) or ""
        head, _, tail = spelled.partition(".")
        if head == "velox" and tail in _SCALARS:
            return tail
        mark = self.mark_of(node)
        return self.translate(mark).scalar if mark is not None else None

    def drop(
        self,
        original: cst.FunctionDef | cst.ClassDef,
        updated: cst.FunctionDef | cst.ClassDef,
        dropped: Sequence[cst.Decorator],
    ) -> cst.FunctionDef | cst.ClassDef:
        """`updated` without the decorators `dropped` names, keeping the comments among them.

        A comment written between two decorators belongs to the one below it, so dropping that
        decorator hands the comment down to whichever decorator, or `def`, follows.
        """
        kept: list[cst.Decorator] = []
        pending: list[cst.EmptyLine] = []
        for before, after in zip(original.decorators, updated.decorators, strict=True):
            if any(before is node for node in dropped):
                pending.extend(after.leading_lines)
                continue
            if pending:
                after = after.with_changes(leading_lines=[*pending, *after.leading_lines])
                pending = []
            kept.append(after)
        return updated.with_changes(
            decorators=kept,
            lines_after_decorators=[*pending, *updated.lines_after_decorators],
        )


class _Parametrize(_MarkPass):
    """VX101: `@pytest.mark.parametrize`, carrying the dump's own case ids where it can.

    Stacked marks are also reversed, which is what keeps a composed case id where pytest put it.
    pytest applies decorators bottom-up, so the innermost axis is registered first, varies slowest
    and is written first in the id; velox reads its own list top-down, outermost first. Reversing
    the decorators makes the two agree on both the id and the order the cases come in, and it
    changes nothing else — the cases are the same product either way.
    """

    CODE = "VX101"

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._rewritten: dict[str | None, list[int]] = {}

    def leave_Decorator(
        self, original_node: cst.Decorator, updated_node: cst.Decorator
    ) -> cst.Decorator:
        rewritten = self.rewrite(original_node, updated_node)
        if rewritten is not updated_node:
            self._rewritten.setdefault(self.qualname, []).append(id(rewritten))
        return rewritten

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        # Keyed by what this pass wrote, never by what the source already says, so a second run
        # over a converted module finds nothing to reverse and reverses nothing.
        written = set(self._rewritten.pop(self.qualname, ()))
        positions = [
            index
            for index, decorator in enumerate(updated_node.decorators)
            if id(decorator) in written
        ]
        if len(positions) < 2:
            return updated_node
        decorators = list(updated_node.decorators)
        for position, source in zip(positions, reversed(positions), strict=True):
            decorators[position] = updated_node.decorators[source].with_changes(
                leading_lines=updated_node.decorators[position].leading_lines,
                whitespace_after_at=updated_node.decorators[position].whitespace_after_at,
                trailing_whitespace=updated_node.decorators[position].trailing_whitespace,
            )
        self.record(
            f"{len(positions)} stacked `parametrize` marks are reversed, so the axis pytest "
            "varied slowest still varies slowest and the composed case ids do not move."
        )
        return updated_node.with_changes(decorators=decorators)


class _StringSkipIf(_MarkPass):
    """VX103: `@pytest.mark.skipif` with a string condition, as a zero-argument lambda."""

    CODE = "VX103"

    def leave_Decorator(
        self, original_node: cst.Decorator, updated_node: cst.Decorator
    ) -> cst.Decorator:
        return self.rewrite(original_node, updated_node)


class _Skips(_MarkPass):
    """VX104: `@pytest.mark.skip`, `@pytest.mark.skipif` on a value, and a plain
    `@pytest.mark.xfail`."""

    CODE = "VX104"

    def leave_Decorator(
        self, original_node: cst.Decorator, updated_node: cst.Decorator
    ) -> cst.Decorator:
        return self.rewrite(original_node, updated_node)


class _UnrunXFail(_MarkPass):
    """VX106: `@pytest.mark.xfail(run=False)`, which is a skip."""

    CODE = "VX106"

    def leave_Decorator(
        self, original_node: cst.Decorator, updated_node: cst.Decorator
    ) -> cst.Decorator:
        return self.rewrite(original_node, updated_node)


class _StrictXFail(_MarkPass):
    """VX107: the suite's `xfail_strict`, written into every `@velox.xfail` that leaves it open.

    This rule reads the velox spelling rather than a pytest one, so it settles what the rules
    ahead of it emitted as well as an `@velox.xfail` already in the file; either way the `strict=`
    it writes is what makes a second pass over the file find nothing to do.
    """

    CODE = "VX107"

    def leave_Decorator(
        self, original_node: cst.Decorator, updated_node: cst.Decorator
    ) -> cst.Decorator:
        if not self.context.xfail_strict or self.is_blocked or not self.is_test:
            return updated_node
        call = updated_node.decorator
        if not isinstance(call, cst.Call) or dotted(call.func) != "velox.xfail":
            return updated_node
        if keyword(call, "strict") is not None or starred(call):
            return updated_node
        self.record(
            f"`{render(call)}` carries the suite's `xfail_strict`, so an unexpected pass fails "
            "the test."
        )
        return updated_node.with_changes(decorator=with_keyword(call, "strict", cst.Name("True")))


class _CustomMark(_MarkPass):
    """VX109: a mark the suite made up, as a selection tag."""

    CODE = "VX109"

    def leave_Decorator(
        self, original_node: cst.Decorator, updated_node: cst.Decorator
    ) -> cst.Decorator:
        return self.rewrite(original_node, updated_node)


class _DroppedMark(_MarkPass):
    """A mark with no velox decorator to become: the rewrite is removing it."""

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if self.is_blocked or not self.is_test:
            return updated_node
        dropped: list[cst.Decorator] = []
        for decorator in original_node.decorators:
            mark = self.mark_of(decorator.decorator)
            if mark is None:
                continue
            translated = self.translate(mark, qualname=self.qualname)
            if translated.owner == self.CODE and not translated.refused:
                dropped.append(decorator)
                self.record(translated.message)
        if not dropped:
            return updated_node
        result = self.drop(original_node, updated_node, dropped)
        assert isinstance(result, cst.FunctionDef)
        return result


class _UseFixtures(_DroppedMark):
    """VX009: `@pytest.mark.usefixtures`, which a `velox.use(...)` declaration replaces.

    A wiring row rather than a mark one, and here anyway: the construct is a mark wherever it is
    written, and the declaration that answers for it is placed by `convert.declarations`, which
    reads the dump rather than the source. This rule only takes the mark away.
    """

    CODE = "VX009"


class _AsyncMark(_DroppedMark):
    """VX110: `@pytest.mark.asyncio` and `@pytest.mark.anyio`, which go away."""

    CODE = "VX110"


class _Indirect(_DroppedMark):
    """VX007: `@pytest.mark.parametrize(..., indirect=True)`, whose values the fixture takes over.

    A wiring row rather than a mark one, and here for the same reason `VX009` is: the construct is
    a mark, and what answers for it is written elsewhere — the `params=` and the ids go onto the
    fixture during the wiring swap, from the case list the plan read out of the dump. This rule
    only takes the mark away.
    """

    CODE = "VX007"


class _Timeout(_MarkPass):
    """VX111: `@pytest.mark.timeout`."""

    CODE = "VX111"

    def leave_Decorator(
        self, original_node: cst.Decorator, updated_node: cst.Decorator
    ) -> cst.Decorator:
        return self.rewrite(original_node, updated_node)


@dataclass(frozen=True, slots=True)
class _Group:
    """One place a mark was written for a group of tests, rather than for one test.

    `scope` is the class the group covers, or `""` for a whole module. `carrier` is what holds the
    marks and goes away once they are distributed: the `pytestmark` statement, or the class whose
    decorators they are.
    """

    scope: str
    subject: str
    carrier: cst.SimpleStatementLine | cst.ClassDef
    marks: tuple[cst.BaseExpression, ...]


class _PytestMarks(_MarkPass):
    """VX115: a `pytestmark`, or a mark on a `class Test*`, as a decorator on each test it reached.

    Every mark in one group is translated or none is: a group holding a mark no rule writes stays
    where it is, since distributing the rest would leave the assignment behind for a second pass to
    distribute again.

    The rules that write a decorator have already run by the time this one does, so the marks it
    distributes carry the suite's `xfail_strict` themselves.
    """

    CODE = "VX115"

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._reaching: dict[str, list[cst.BaseExpression]] = {}
        self._removed: list[cst.CSTNode] = []

    def visit_Module(self, node: cst.Module) -> None:
        # Decided here, before anything is rewritten: what a group reaches is `Context`'s answer,
        # not the tree's, so a statement can be removed on the way past it.
        for group in self._groups(node):
            self._plan(group)

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if self.is_blocked or not self.is_test:
            return updated_node
        qualname = self.qualname or ""
        marks = self._reaching.get(qualname)
        if not marks:
            return updated_node
        scalars = {scalar for d in updated_node.decorators if (scalar := self.scalar_of(d))}
        added: list[cst.Decorator] = []
        for mark in marks:
            read = self.mark_of(mark)
            if read is None:
                continue
            translated = self.translate(
                read, qualname=qualname, strict_default=self.context.xfail_strict
            )
            if translated.expression is None:
                continue
            if translated.scalar is not None and translated.scalar in scalars:
                self.record(
                    f"`{render(mark)}` is not distributed onto `{qualname}`, which carries a "
                    f"`@velox.{translated.scalar}` of its own already.",
                    code="VX113",
                    qualname=qualname,
                )
                continue
            if translated.scalar is not None:
                scalars.add(translated.scalar)
            added.append(cst.Decorator(decorator=translated.expression))
        if not added:
            return updated_node
        return updated_node.with_changes(decorators=[*added, *updated_node.decorators])

    def leave_ClassDef(
        self, original_node: cst.ClassDef, updated_node: cst.ClassDef
    ) -> cst.ClassDef:
        dropped = [
            decorator
            for decorator in original_node.decorators
            if any(decorator is node for node in self._removed)
        ]
        if not dropped:
            return updated_node
        result = self.drop(original_node, updated_node, dropped)
        assert isinstance(result, cst.ClassDef)
        return result

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        kept, trailing = self._prune(original_node.body, updated_node.body)
        if trailing is None:
            return updated_node
        return updated_node.with_changes(body=kept, footer=[*trailing, *updated_node.footer])

    def leave_IndentedBlock(
        self, original_node: cst.IndentedBlock, updated_node: cst.IndentedBlock
    ) -> cst.IndentedBlock:
        kept, trailing = self._prune(original_node.body, updated_node.body)
        if trailing is None:
            return updated_node
        return updated_node.with_changes(body=kept, footer=[*trailing, *updated_node.footer])

    # --- what a group is, and what it reaches --------------------------------------------------

    def _groups(self, module: cst.Module) -> list[_Group]:
        found: list[_Group] = []
        self._walk(module.body, "", found)
        return found

    def _walk(self, body: Sequence[cst.CSTNode], scope: str, found: list[_Group]) -> None:
        for statement in body:
            if isinstance(statement, cst.SimpleStatementLine):
                marks = _pytestmark(statement)
                if marks is not None:
                    found.append(
                        _Group(
                            scope=scope,
                            subject=f"`pytestmark` in `{scope}`" if scope else "`pytestmark`",
                            carrier=statement,
                            marks=marks,
                        )
                    )
            elif isinstance(statement, cst.ClassDef):
                inner = f"{scope}.{statement.name.value}" if scope else statement.name.value
                marks = tuple(
                    decorator.decorator
                    for decorator in statement.decorators
                    if self.mark_of(decorator.decorator) is not None
                )
                if marks:
                    found.append(
                        _Group(
                            scope=inner,
                            subject=f"the mark on `class {statement.name.value}`",
                            carrier=statement,
                            marks=marks,
                        )
                    )
                self._walk(statement.body.body, inner, found)

    def _plan(self, group: _Group) -> None:
        """Record what becomes of one group, and note the marks the tests it reaches receive."""
        where = group.scope or None
        for mark in group.marks:
            read = self.mark_of(mark)
            translated = (
                self.translate(read, strict_default=self.context.xfail_strict)
                if read is not None
                else None
            )
            if translated is None or translated.owner is None or translated.refused:
                self.record(
                    f"{group.subject} stays where it is: "
                    + (
                        translated.message
                        if translated is not None
                        else f"`{render(mark)}` is not a mark."
                    ),
                    code=translated.code if translated is not None else self.CODE,
                    qualname=where,
                )
                return
            if translated.owner == "VX101":
                self.record(
                    f"{group.subject} stays where it is: `{render(mark)}` names its cases' ids "
                    "per test, so distributing it would change them.",
                    code="VX101",
                    qualname=where,
                )
                return
        reached = sorted(_in_scope(self.context.tests, group.scope))
        targets = [test for test in reached if test not in self.context.blocked]
        if not targets:
            self.record(
                f"{group.subject} stays where it is: it reaches no test this file converts.",
                qualname=where,
            )
            return
        for test in targets:
            self._reaching.setdefault(test, []).extend(group.marks)
        if isinstance(group.carrier, cst.ClassDef):
            # The class itself stays; only the decorators whose marks were distributed go.
            self._removed.extend(
                decorator
                for decorator in group.carrier.decorators
                if any(decorator.decorator is mark for mark in group.marks)
            )
        else:
            self._removed.append(group.carrier)
        left = [test for test in reached if test not in targets]
        note = (
            f" `{'`, `'.join(left)}` keeps its pytest source, and its marks with it."
            if left
            else ""
        )
        self.record(
            f"{group.subject} becomes a decorator on `{'`, `'.join(targets)}`.{note}",
            qualname=where,
        )

    def _prune(
        self, original: Sequence[cst.BaseStatement], updated: Sequence[cst.BaseStatement]
    ) -> tuple[list[cst.BaseStatement], list[cst.EmptyLine] | None]:
        """`updated` without the statements this pass distributed, and any trailing comments.

        A comment above a removed statement moves down to the statement that follows it; the blank
        lines around it go with the statement, so removing one closes the gap it left.
        """
        if len(original) != len(updated) or not any(
            any(before is node for node in self._removed) for before in original
        ):
            return list(updated), None
        kept: list[cst.BaseStatement] = []
        pending: list[cst.EmptyLine] = []
        opened = False
        for before, after in zip(original, updated, strict=True):
            if any(before is node for node in self._removed):
                pending.extend(line for line in leading(after) if line.comment is not None)
                opened = not kept
                continue
            if opened:
                # The removed statement stood at the top of the block, so the blank line under it
                # would be left opening the block.
                after = after.with_changes(leading_lines=_after_blanks(leading(after)))
                opened = False
            if pending:
                after = after.with_changes(leading_lines=[*leading(after), *pending])
                pending = []
            kept.append(after)
        return kept, pending


def _after_blanks(lines: Sequence[cst.EmptyLine]) -> list[cst.EmptyLine]:
    """`lines` without the blank ones it starts with, keeping every comment."""
    kept = list(lines)
    while kept and kept[0].comment is None:
        kept.pop(0)
    return kept


def _in_scope(tests: Iterable[str], scope: str) -> set[str]:
    """The qualnames `scope` covers: a class's own methods, or a whole module's tests."""
    if not scope:
        return set(tests)
    return {test for test in tests if test.startswith(f"{scope}.")}


def _pytestmark(statement: cst.SimpleStatementLine) -> tuple[cst.BaseExpression, ...] | None:
    """The marks a `pytestmark` statement holds, or `None` when it is not one.

    A line that assigns anything else alongside `pytestmark` is not one: removing it would take
    the other assignment with it.
    """
    if len(statement.body) != 1:
        return None
    assignment = statement.body[0]
    if isinstance(assignment, cst.Assign):
        targets = [target.target for target in assignment.targets]
        value = assignment.value
    elif isinstance(assignment, cst.AnnAssign) and assignment.value is not None:
        targets = [assignment.target]
        value = assignment.value
    else:
        return None
    if not all(isinstance(target, cst.Name) and target.value == "pytestmark" for target in targets):
        return None
    if isinstance(value, cst.List | cst.Tuple):
        if any(not isinstance(element, cst.Element) for element in value.elements):
            return None
        return tuple(element.value for element in value.elements)
    return (value,)


def _argnames(node: cst.BaseExpression) -> tuple[str, ...] | None:
    """The names a `parametrize` axis binds, as written, or `None` when they are not literal."""
    text = literal_text(node)
    if text is not None:
        return tuple(name.strip() for name in text.split(",") if name.strip())
    if isinstance(node, cst.List | cst.Tuple):
        names = [literal_text(element.value) for element in node.elements]
        if all(name is not None for name in names):
            return tuple(name for name in names if name is not None)
    return None


def _case(values: Sequence[cst.Arg], *, arity: int | None) -> cst.BaseExpression | None:
    """One `pytest.param`'s values as a case: the value itself for one argname, a tuple for more."""
    if not values:
        return None
    if len(values) == 1 and (arity is None or arity == 1):
        return values[0].value
    if arity is not None and len(values) != arity:
        return None
    return _tuple([value.value for value in values])


def _case_count(values: cst.BaseExpression) -> int | None:
    """How many cases `values` holds, or `None` when the source does not say."""
    if not isinstance(values, cst.List | cst.Tuple | cst.Set):
        return None
    if any(not isinstance(element, cst.Element) for element in values.elements):
        return None
    return len(values.elements)


def _tuple(values: Sequence[cst.BaseExpression]) -> cst.Tuple:
    return cst.Tuple(elements=[cst.Element(value=value) for value in values])


def _strict(strict: bool) -> list[cst.Arg]:
    return [argument(cst.Name("True"), "strict")] if strict else []


def _root_names(node: cst.CSTNode) -> frozenset[str]:
    """Every name an expression reads at the head of an attribute chain."""
    collector = _Roots()
    node.visit(collector)
    return frozenset(collector.names)


class _Roots(cst.CSTVisitor):
    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def visit_Name(self, node: cst.Name) -> None:
        self.names.add(node.value)

    def visit_Attribute(self, node: cst.Attribute) -> bool:
        node.value.visit(self)
        return False


def _is_bool(node: cst.BaseExpression) -> bool:
    return isinstance(node, cst.Name) and node.value in ("True", "False")


def _is_false(node: cst.BaseExpression) -> bool:
    return isinstance(node, cst.Name) and node.value == "False"


def _is_nonpositive(node: cst.BaseExpression) -> bool:
    """Whether `node` is a literal number velox's `timeout` refuses."""
    if isinstance(node, cst.UnaryOperation) and isinstance(node.operator, cst.Minus):
        return isinstance(node.expression, cst.Integer | cst.Float)
    if isinstance(node, cst.Integer | cst.Float):
        return float(node.evaluated_value) <= 0
    return False


RULES: tuple[TransformerRule, ...] = (
    rule(_Indirect),
    rule(_UseFixtures),
    rule(_Parametrize),
    rule(_StringSkipIf),
    rule(_Skips),
    rule(_UnrunXFail),
    rule(_StrictXFail),
    rule(_CustomMark),
    rule(_AsyncMark),
    rule(_Timeout),
    rule(_PytestMarks),
)
