"""The body rules: what a test body's own calls into pytest become.

A builtin fixture is reached through the name a body spells it with, so these rules recognize
`capsys` and `caplog` as well as the `capture` and `log_records` they become — whichever of the two
a body carries by the time a rule reads it — and only where an enclosing `def` takes that name as a
parameter, since a local called `caplog` is not pytest's. Renaming the parameter itself is wiring's.

`pytest.raises` and `pytest.approx` are read through `QualifiedNameProvider` instead, and each is
rewritten only in the shapes velox has: a `raises` entered by a `with` or called with the
exception it expects and the callable it wraps, and an `approx` over a scalar, a list, a tuple, or
a dict. Anything else is left as it was written, with the row that refuses it recorded.

`pytest.skip`/`pytest.fail`, called as a statement rather than read off a mark, become a `raise`
of `velox.Skipped`/`velox.Failed` -- the runtime counterparts of the `skip`/`xfail` marks, for a
body that decides mid-run rather than at collection. `pytest.xfail` the same way stays refused:
there is no runtime target for an expectation `@velox.xfail(...)` only ever decides once, ahead of
the test running at all.

The two `request` rules are the exception to a rule deciding anything: what a name means and
whether a teardown can move are questions about the whole suite and about a factory's shape, so
the plan answers them from the audit and these rules rewrite only the sites it names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import libcst as cst

from velox_migrate.audit.sources import APPROX_KINDS, approx_nested
from velox_migrate.convert.rules import (
    Context,
    RuleTransformer,
    TransformerRule,
    argument,
    double_starred,
    keyword,
    keywords,
    leading,
    literal_text,
    positional,
    render,
    rule,
    starred,
    velox,
)

CAPTURE = "capture"
LOG_RECORDS = "log_records"
REQUEST = "request"

# The two names one builtin fixture answers to while a file is being converted.
_CAPTURE_NAMES = (CAPTURE, "capsys")
_LOG_NAMES = (LOG_RECORDS, "caplog")

# The attributes `velox.log_records` carries under the same names `caplog` did, mapped to the
# code each rename files under: `records`/`messages` are this class's own default (VX204);
# `text`/`record_tuples` are VX206's promoted rename onto the same object.
_LOG_ATTRS: Mapping[str, str | None] = {
    "records": None,
    "messages": None,
    "text": "VX206",
    "record_tuples": "VX206",
}

# What `pytest.approx`'s positional arguments are, after the value itself.
_APPROX_POSITIONAL = ("rel", "abs", "nan_ok")

# The shapes of `pytest.approx`'s first argument that convert under VX213 rather than the default
# VX212 (a scalar). A literal's own elements are what `approx_nested` walks; a comprehension's
# runtime shape cannot be inspected that way, so it converts unchecked.
_APPROX_CONTAINER_SHAPES = (cst.List, cst.ListComp, cst.Tuple, cst.Dict, cst.DictComp)

# `velox.raises` refuses to catch a cancellation, which is how a timeout stops a runaway test, so
# it refuses every type a cancellation is an instance of.
_UNCATCHABLE = frozenset(
    {
        "asyncio.CancelledError",
        "asyncio.exceptions.CancelledError",
        "BaseException",
        "builtins.BaseException",
    }
)


class _BodyPass(RuleTransformer):
    """Reading the builtin fixtures a body holds, shared by the rules over them."""

    def receiver(self, node: cst.BaseExpression, names: Sequence[str]) -> str | None:
        """Which of `names` `node` is, as a fixture an enclosing `def` takes rather than a local."""
        if not isinstance(node, cst.Name):
            return None
        return node.value if node.value in names and self.takes(node.value) else None

    def method(self, call: cst.Call, names: Sequence[str], attribute: str) -> cst.Attribute | None:
        """`call` as `<one of names>.<attribute>(...)`, and its own `func` where it is."""
        func = call.func
        if not isinstance(func, cst.Attribute) or func.attr.value != attribute:
            return None
        return func if self.receiver(func.value, names) is not None else None


class _Capture(_BodyPass):
    """VX201: `capsys.readouterr()`, whose `.out` and `.err` are live on `velox.capture`.

    A `readouterr()` whose result is read as an object — an attribute off it, or a name bound to it
    — is that object, so the call goes away and the reads stay. Every other use is refused: the
    call's other job was clearing the buffer, and `capture` is cumulative, so a rewrite that
    dropped the clearing would quietly change what the next assertion sees.
    """

    CODE = "VX201"

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._reads: dict[str, int] = {}

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        if self.is_blocked or self.method(original_node, _CAPTURE_NAMES, "readouterr") is None:
            return updated_node
        if original_node.args:
            return updated_node
        qualname = self.qualname or ""
        self._reads[qualname] = self._reads.get(qualname, 0) + 1
        parent = self.parent(original_node)
        if isinstance(parent, cst.Attribute) and parent.attr.value in ("out", "err"):
            return self._read(original_node, cst.Name(CAPTURE))
        if isinstance(parent, cst.Assign):
            targets = [target.target for target in parent.targets]
            if len(targets) == 1 and isinstance(targets[0], cst.Name):
                return self._read(original_node, cst.Name(CAPTURE))
            if len(targets) == 1 and isinstance(targets[0], cst.Tuple | cst.List):
                bound = targets[0].elements
                if len(bound) == 2:
                    return self._read(original_node, _out_and_err())
        self.record(
            f"`{render(original_node)}` is left as it is: its result is neither read as an object "
            "nor unpacked, and `velox.capture` is not the snapshot `readouterr()` returns."
        )
        return updated_node

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        reads = self._reads.get(self.qualname or "", 0)
        if reads > 1:
            self.record(
                f"`readouterr()` is read {reads} times here, and `velox.capture` is cumulative: "
                "the second read sees the first read's output too.",
                code="VX202",
            )
        return updated_node

    def _read(self, original: cst.Call, replacement: cst.BaseExpression) -> cst.BaseExpression:
        self.record(f"`{render(original)}` becomes `{CAPTURE}`, whose `.out` and `.err` are live.")
        return replacement


class _LogRecords(_BodyPass):
    """VX204: `caplog.records` and `caplog.messages`, the same two names on `velox.log_records`.
    VX206: `caplog.text`, `caplog.record_tuples` and `caplog.clear()`, renamed the same way but
    filed under their own code since VX204's row is specifically about `records`/`messages`.
    """

    CODE = "VX204"

    def leave_Attribute(
        self, original_node: cst.Attribute, updated_node: cst.Attribute
    ) -> cst.BaseExpression:
        attr = original_node.attr.value
        if self.is_blocked or attr not in _LOG_ATTRS:
            return updated_node
        held = self.receiver(original_node.value, _LOG_NAMES)
        if held is None or held == LOG_RECORDS:
            return updated_node
        self.record(
            f"`{render(original_node)}` becomes `{LOG_RECORDS}.{attr}`.", code=_LOG_ATTRS[attr]
        )
        return updated_node.with_changes(value=cst.Name(LOG_RECORDS))

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        func = original_node.func
        if (
            self.is_blocked
            or not isinstance(func, cst.Attribute)
            or func.attr.value != "clear"
            or original_node.args
        ):
            return updated_node
        held = self.receiver(func.value, _LOG_NAMES)
        if held is None or held == LOG_RECORDS:
            return updated_node
        self.record(f"`{render(original_node)}` becomes `{LOG_RECORDS}.clear()`.", code="VX206")
        return updated_node.with_changes(func=func.with_changes(value=cst.Name(LOG_RECORDS)))


class _SetLevel(_BodyPass):
    """VX205: `caplog.set_level(...)`, as a `with` block over the rest of the test.

    `LogRecords.set_level` does nothing until it is entered, so the statement has to become a
    block, and only a statement standing on its own in a body can become one: the rest of that body
    is what the block holds. A `set_level` under an `if`, inside another block, or with nothing
    after it is left as it was written — and so is a second one in the same body, which the first
    one's block now holds.
    """

    CODE = "VX205"

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if self.is_blocked:
            return updated_node
        body = updated_node.body
        if not isinstance(body, cst.IndentedBlock):
            return updated_node
        statements = list(body.body)
        found = [
            (index, call)
            for index, statement in enumerate(statements)
            if (call := self._set_level(statement)) is not None
        ]
        if not found:
            return updated_node
        for _, call in found[1:]:
            self.record(
                f"`{render(call)}` is left as it is: the block above it already holds the rest of "
                "this test, and a level raised inside one is not this rule's to nest."
            )
        index, call = found[0]
        rest = statements[index + 1 :]
        if not rest:
            self.record(
                f"`{render(call)}` is left as it is: nothing follows it in this test, so there is "
                "nothing for the block to hold."
            )
            return updated_node
        block = cst.With(
            items=[cst.WithItem(item=self._entered(call))],
            body=cst.IndentedBlock(body=rest),
            leading_lines=leading(statements[index]),
        )
        self.record(
            f"`{render(call)}` becomes a `with` block over the rest of this test, and the logger's "
            "level is process-global while it holds."
        )
        return updated_node.with_changes(body=body.with_changes(body=[*statements[:index], block]))

    def _set_level(self, statement: cst.BaseStatement) -> cst.Call | None:
        """`statement` as a `set_level` call standing on its own in a body."""
        if not isinstance(statement, cst.SimpleStatementLine) or len(statement.body) != 1:
            return None
        expression = statement.body[0]
        if not isinstance(expression, cst.Expr) or not isinstance(expression.value, cst.Call):
            return None
        call = expression.value
        return call if self.method(call, _LOG_NAMES, "set_level") is not None else None

    def _entered(self, call: cst.Call) -> cst.Call:
        """The same call on `log_records`, with a second positional argument named."""
        func = call.func
        assert isinstance(func, cst.Attribute)
        given = positional(call)
        named = given[1] if len(given) > 1 else None
        args = [argument(arg.value, "logger") if arg is named else arg for arg in call.args]
        return call.with_changes(func=func.with_changes(value=cst.Name(LOG_RECORDS)), args=args)


class _Raises(_BodyPass):
    """VX209: `pytest.raises` where a `with` enters it, and VX210: the callable form
    `pytest.raises(E, func, *args, **kwargs)` — both are shapes velox's `raises` has too, so both
    rewrite to `velox.raises` unchanged but for the name. A `pytest.raises(E)` that is neither —
    entered by nothing, called with no second positional argument — is a raises object being
    stashed for later, which still reports VX210: there is no `with` and no `func` for a rewrite
    to key off of. A callable form passing `match=` is left alone too: pytest forwards it to
    `func` there, but `velox.raises` always intercepts it, so the two forms disagree on what the
    call means — and so is one unpacking a `**mapping`, since it might carry a `match` key the
    rewrite has no way to see.
    """

    CODE = "VX209"

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        if self.is_blocked or "pytest.raises" not in self.names(original_node):
            return updated_node
        if isinstance(self.parent(original_node), cst.WithItem):
            return self._with_entered(original_node, updated_node)
        return self._callable(original_node, updated_node)

    def _with_entered(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        given = positional(original_node)
        if starred(original_node) or len(given) != 1:
            self.record(
                f"`{render(original_node)}` is left as it is: `velox.raises` takes the expected "
                "exception and nothing else positionally.",
                code="VX210",
            )
            return updated_node
        uncatchable = self._uncatchable(given[0].value)
        if uncatchable is not None:
            self.record(
                f"`{render(original_node)}` is left as it is: `velox.raises` refuses "
                f"`{uncatchable}`, since cancellation is how velox enforces a timeout.",
                code="VX211",
            )
            return updated_node
        unknown = keywords(original_node) - {"match"}
        if unknown:
            self.record(
                f"`{render(original_node)}` is left as it is: `velox.raises` has no "
                f"`{'`, `'.join(sorted(unknown))}`.",
            )
            return updated_node
        self.record(f"`{render(original_node)}` becomes `velox.raises`.")
        return updated_node.with_changes(func=velox("raises"))

    def _callable(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        """`pytest.raises(E, func, *args, **kwargs)`, not entered by a `with`."""
        given = positional(original_node)
        if len(given) < 2:
            self.record(
                f"`{render(original_node)}` is left as it is: `velox.raises` is a context "
                "manager, and this call is not entered by a `with`.",
                code="VX210",
            )
            return updated_node
        if "match" in keywords(original_node) or double_starred(original_node):
            self.record(
                f"`{render(original_node)}` is left as it is: pytest forwards `match` to `func` "
                "in this form, but `velox.raises` always intercepts it to match the exception, "
                "and a `**` unpack might carry one where the rewrite can't see it.",
                code="VX210",
            )
            return updated_node
        uncatchable = self._uncatchable(given[0].value)
        if uncatchable is not None:
            self.record(
                f"`{render(original_node)}` is left as it is: `velox.raises` refuses "
                f"`{uncatchable}`, since cancellation is how velox enforces a timeout.",
                code="VX211",
            )
            return updated_node
        self.record(f"`{render(original_node)}` becomes `velox.raises`.", code="VX210")
        return updated_node.with_changes(func=velox("raises"))

    def _uncatchable(self, expected: cst.BaseExpression) -> str | None:
        """Which of the types `velox.raises` refuses `expected` names, if it names one."""
        candidates = [expected]
        if isinstance(expected, cst.Tuple):
            candidates = [
                element.value for element in expected.elements if isinstance(element, cst.Element)
            ]
        for candidate in candidates:
            named = sorted(self.names(candidate) & _UNCATCHABLE)
            if named:
                return named[0]
        return None


class _Approx(_BodyPass):
    """VX212: `pytest.approx` over a scalar, which is the default this class's own `CODE` files
    under. VX213: `pytest.approx` over a list, a tuple, or a dict — an explicit `code=` override
    on every `record` below, used whenever the first argument has one of those shapes (its own
    comprehension forms included), success and refusal alike; a list, tuple, dict, or set nested
    one level inside a list/tuple/dict literal is one such refusal, since `velox.approx` only
    walks one level. VX221: a set, a set comprehension, a generator expression, or a numpy array,
    which stay refused — there is no position to compare any of those by.
    """

    CODE = "VX212"

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        if self.is_blocked or "pytest.approx" not in self.names(original_node):
            return updated_node
        given = positional(original_node)
        code = self._code(given[0].value) if given else None
        if starred(original_node) or not given:
            self.record(
                f"`{render(original_node)}` is left as it is: `velox.approx` takes the expected "
                "value itself.",
                code=code,
            )
            return updated_node
        expected = given[0].value
        kind = self._kind(expected)
        if kind is not None:
            self.record(
                f"`{render(original_node)}` is left as it is: it is given {kind}, and "
                "`velox.approx` has no position to compare it by.",
                code="VX221",
            )
            return updated_node
        nested = approx_nested(expected)
        if nested is not None:
            self.record(
                f"`{render(original_node)}` is left as it is: `{render(nested)}` is nested "
                "inside it, and `velox.approx` has no position to compare a nested container by.",
                code=code,
            )
            return updated_node
        unknown = keywords(original_node) - set(_APPROX_POSITIONAL)
        if unknown or len(given) > 1 + len(_APPROX_POSITIONAL):
            self.record(
                f"`{render(original_node)}` is left as it is: `velox.approx` takes `rel`, `abs` "
                "and `nan_ok`.",
                code=code,
            )
            return updated_node
        args = list(original_node.args)
        for position, name in zip(given[1:], _APPROX_POSITIONAL, strict=False):
            if keyword(original_node, name) is not None:
                self.record(
                    f"`{render(original_node)}` is left as it is: it passes `{name}` twice.",
                    code=code,
                )
                return updated_node
            args = [argument(arg.value, name) if arg is position else arg for arg in args]
        self.record(f"`{render(original_node)}` becomes `velox.approx`.", code=code)
        return updated_node.with_changes(func=velox("approx"), args=args)

    def _code(self, expected: cst.BaseExpression) -> str | None:
        """`"VX213"` where `expected` is list/tuple/dict shaped, else `None` (the default
        `VX212`, a scalar)."""
        return "VX213" if isinstance(expected, _APPROX_CONTAINER_SHAPES) else None

    def _kind(self, expected: cst.BaseExpression) -> str | None:
        """What `expected` is, where it is something `velox.approx` does not compare."""
        kind = APPROX_KINDS.get(type(expected))
        if kind is not None:
            return kind
        if isinstance(expected, cst.Call) and any(
            name.split(".")[0] in ("np", "numpy") for name in self.names(expected)
        ):
            return "a `numpy` array"
        return None


class _Imperative(_BodyPass):
    """VX214: `pytest.skip(reason)`/`pytest.fail(reason)`, called as a bare statement, becoming
    `raise velox.Skipped(reason)`/`raise velox.Failed(reason)` -- the two are `BaseException`s a
    body raises directly, not decorators like `velox.skip`/`velox.xfail`, whose own row this
    would otherwise collide with (calling a decorator factory in a body builds a decorator and
    discards it, turning a conditional skip into a silent pass). VX223: `pytest.xfail(...)` the
    same way, which stays refused -- there is no runtime target for it, since `@velox.xfail`'s
    condition is decided once, at collection, before the test has run at all.

    Only the reason itself carries over. `reason=`/`fail`'s deprecated `msg=` become a plain
    positional argument either way: `Skipped`/`Failed` are bare `BaseException`s, and raising one
    with a keyword argument raises `TypeError` instead of the outcome it names. A call passing
    `allow_module_level=` (skip's own import-time form) or `pytrace=` (fail), an unknown keyword,
    a `**` unpack that might carry one, or more than one candidate reason (a positional alongside
    a keyword, or both of fail's two spellings) is left as it is -- none of those has a velox
    equivalent, or a reason this rule can pick out on its own.
    """

    CODE = "VX214"

    _TARGETS: Mapping[str, str] = {"pytest.skip": "Skipped", "pytest.fail": "Failed"}
    _REASON_KEYWORDS: Mapping[str, frozenset[str]] = {
        "pytest.skip": frozenset({"reason"}),
        "pytest.fail": frozenset({"reason", "msg"}),
    }

    def leave_Expr(self, original_node: cst.Expr, updated_node: cst.Expr) -> cst.BaseSmallStatement:
        call = original_node.value
        if self.is_blocked or not isinstance(call, cst.Call):
            return updated_node
        name = next(
            (n for n in ("pytest.skip", "pytest.fail", "pytest.xfail") if n in self.names(call)),
            None,
        )
        if name is None:
            return updated_node
        if name == "pytest.xfail":
            self.record(
                f"`{render(call)}` is left as it is: there is no runtime target for it -- "
                "`@velox.xfail(...)`'s condition is decided once at collection, before the test "
                "has run.",
                code="VX223",
            )
            return updated_node
        args = self._args(call, name)
        if args is None:
            return updated_node
        target = self._TARGETS[name]
        self.record(f"`{render(call)}` becomes `raise velox.{target}(...)`.")
        return cst.Raise(exc=cst.Call(func=velox(target), args=args))

    def _args(self, call: cst.Call, name: str) -> list[cst.Arg] | None:
        """`call`'s reason as zero or one positional argument for `velox.Skipped`/`Failed`, or
        `None` where the shape is left alone (having already recorded why)."""
        given = positional(call)
        allowed = self._REASON_KEYWORDS[name]
        reason_kwargs = [kw for kw in sorted(allowed) if keyword(call, kw) is not None]
        unknown = keywords(call) - allowed
        if starred(call) or unknown or len(given) > 1 or bool(given) + len(reason_kwargs) > 1:
            self.record(
                f"`{render(call)}` is left as it is: `velox.{self._TARGETS[name]}` takes only "
                "the reason, positionally."
            )
            return None
        if given:
            return [argument(given[0].value)]
        if reason_kwargs:
            arg = keyword(call, reason_kwargs[0])
            assert arg is not None
            return [argument(arg.value)]
        return []


class _GetFixtureValue(_BodyPass):
    """VX011: `request.getfixturevalue("name")`, which is a parameter the signature grows.

    The name is a literal, so the dependency is as static as a parameter would have been — and the
    plan has already resolved it to one object and told the wiring swap to inject it. What is left
    here is the call itself, which becomes the name that parameter binds.
    """

    CODE = "VX011"

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        if self.is_blocked or self.method(original_node, (REQUEST,), "getfixturevalue") is None:
            return updated_node
        given = positional(original_node)
        wanted = literal_text(given[0].value) if given else None
        param = self.context.requested.get(self.qualname or "", {}).get(wanted or "")
        if param is None:
            return updated_node
        self.record(f"`{render(original_node)}` becomes the injected parameter `{param}`.")
        return cst.Name(param)


class _Finalizers(_BodyPass):
    """VX013: `request.addfinalizer(fn)`, which is what a `yield` fixture's teardown says.

    Every registration in the factory goes, the value it hands over is yielded rather than
    returned, and the callables are called after the `yield` in the reverse of the order they were
    registered in — which is the order pytest runs them in. A factory with nothing to hand over
    yields nothing, since a generator is what a teardown needs, not a value.

    Only a `def` at module level is rewritten, which is the only kind a fixture object is ever
    made of.
    """

    CODE = "VX013"

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if self.is_blocked or not self.at_module_level:
            return updated_node
        if (self.qualname or "") not in self.context.finalizers:
            return updated_node
        body = updated_node.body
        if not isinstance(body, cst.IndentedBlock):
            return updated_node
        registration = _Registrations()
        stripped = body.visit(registration)
        if not registration.registered or not isinstance(stripped, cst.IndentedBlock):
            return updated_node
        statements = list(stripped.body)
        teardown = [_calling(call) for call in reversed(registration.registered)]
        self.record(
            f"`request.addfinalizer` is called {len(registration.registered)} time(s) here, and "
            "becomes the teardown after this fixture's `yield`."
        )
        return updated_node.with_changes(
            body=stripped.with_changes(body=[*_yielding(statements), *teardown])
        )


class _Registrations(cst.CSTTransformer):
    """Takes every `request.addfinalizer(...)` statement out of a body, keeping what it registered.

    A function written inside the body is left alone: what it registers is registered only if
    something calls it, which is not the unconditional shape the plan attributed this site for.

    A removed statement hands the blank lines above it to whatever followed it, so taking a line
    out of a body does not close a gap the author wrote.
    """

    def __init__(self) -> None:
        super().__init__()
        self.registered: list[cst.BaseExpression] = []
        # The statements themselves, not their ids: a block is rebuilt bottom-up, so a node this
        # pass dropped can be collected and its id handed to a node built afterwards.
        self._removing: list[cst.SimpleStatementLine] = []

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        return False

    def leave_SimpleStatementLine(
        self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine
    ) -> cst.SimpleStatementLine:
        call = _registration(updated_node)
        if call is None:
            return updated_node
        given = positional(call)
        if len(given) != 1 or starred(call):
            return updated_node
        self.registered.append(given[0].value)
        self._removing.append(updated_node)
        return updated_node

    def leave_IndentedBlock(
        self, original_node: cst.IndentedBlock, updated_node: cst.IndentedBlock
    ) -> cst.IndentedBlock:
        if not any(self._is_removed(statement) for statement in updated_node.body):
            return updated_node
        kept: list[cst.BaseStatement] = []
        carried: list[cst.EmptyLine] = []
        for statement in updated_node.body:
            if self._is_removed(statement):
                carried = [*carried, *leading(statement)]
                continue
            kept.append(_led(statement, carried) if carried else statement)
            carried = []
        # An empty block renders as `pass`, which is what a `with` whose only statement was a
        # registration becomes; a factory's own body is never left empty, since a `yield` follows.
        return updated_node.with_changes(body=kept)

    def _is_removed(self, statement: cst.BaseStatement) -> bool:
        return any(statement is removed for removed in self._removing)


def _registration(statement: cst.SimpleStatementLine) -> cst.Call | None:
    """`statement` as a lone `request.addfinalizer(...)` call, or `None` for anything else."""
    match statement.body:
        case [
            cst.Expr(
                value=cst.Call(
                    func=cst.Attribute(
                        value=cst.Name(value="request"), attr=cst.Name(value="addfinalizer")
                    )
                ) as call
            )
        ]:
            return call
        case _:
            return None


def _yielding(statements: list[cst.BaseStatement]) -> list[cst.BaseStatement]:
    """`statements` with the value the factory handed back yielded instead of returned.

    A factory that hands nothing back — one ending in a bare `return`, or in no `return` at all —
    yields nothing, since what the teardown needs is a generator rather than a value.
    """
    trailing = statements[-1] if statements else None
    if not isinstance(trailing, cst.SimpleStatementLine) or not isinstance(
        trailing.body[-1], cst.Return
    ):
        return [*statements, cst.SimpleStatementLine(body=[cst.Expr(value=cst.Yield())])]
    yielded = cst.Expr(value=cst.Yield(value=trailing.body[-1].value))
    return [*statements[:-1], trailing.with_changes(body=[*trailing.body[:-1], yielded])]


def _led(statement: cst.BaseStatement, carried: Sequence[cst.EmptyLine]) -> cst.BaseStatement:
    """`statement` with the blank lines a removed statement above it left behind."""
    if not isinstance(statement, cst.SimpleStatementLine | cst.BaseCompoundStatement):
        return statement
    return statement.with_changes(leading_lines=[*carried, *statement.leading_lines])


def _calling(registered: cst.BaseExpression) -> cst.SimpleStatementLine:
    """The finalizer called, parenthesized where the expression that names it needs it."""
    called = (
        registered
        if isinstance(registered, cst.Name | cst.Attribute | cst.Call | cst.Subscript)
        else registered.with_changes(lpar=[cst.LeftParen()], rpar=[cst.RightParen()])
    )
    return cst.SimpleStatementLine(body=[cst.Expr(value=cst.Call(func=called))])


def _out_and_err() -> cst.BaseExpression:
    """`capture.out, capture.err`, which is what unpacking a `readouterr()` bound."""
    return cst.Tuple(
        elements=[
            cst.Element(value=cst.Attribute(value=cst.Name(CAPTURE), attr=cst.Name(part)))
            for part in ("out", "err")
        ],
        lpar=(),
        rpar=(),
    )


RULES: tuple[TransformerRule, ...] = (
    rule(_GetFixtureValue),
    rule(_Finalizers),
    rule(_Capture),
    rule(_LogRecords),
    rule(_SetLevel),
    rule(_Raises),
    rule(_Approx),
    rule(_Imperative),
)
