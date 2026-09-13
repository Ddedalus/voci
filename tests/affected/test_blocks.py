"""Tests for voci._affected.blocks: statement and def blocks, their binds/references/effect
metadata, and their checksums."""

from __future__ import annotations

from voci._affected.blocks import Block, parse_blocks


def _by_qualname(blocks: list[Block], qualname: str) -> Block:
    matches = [b for b in blocks if b.qualname == qualname]
    assert len(matches) == 1, f"expected exactly one block qualname={qualname!r}, got {matches}"
    return matches[0]


def _statement_binding(blocks: list[Block], name: str) -> Block:
    matches = [b for b in blocks if b.qualname is None and name in b.binds]
    assert len(matches) == 1, f"expected one statement block binding {name!r}, got {matches}"
    return matches[0]


# A bare top-level function
# ------------------------------------------------------------------------


def test_a_top_level_def_produces_a_statement_block_and_a_def_block() -> None:
    blocks = parse_blocks("def foo():\n    return 1\n")
    statement = _statement_binding(blocks, "foo")
    assert statement.qualname is None
    assert statement.effect is False
    definition = _by_qualname(blocks, "foo")
    assert definition.binds == frozenset()


def test_changing_the_body_only_changes_the_def_blocks_checksum() -> None:
    before = parse_blocks("def foo():\n    return 1\n")
    after = parse_blocks("def foo():\n    return 2\n")
    assert _statement_binding(before, "foo").checksum == _statement_binding(after, "foo").checksum
    assert _by_qualname(before, "foo").checksum != _by_qualname(after, "foo").checksum


def test_changing_a_default_arg_value_only_changes_the_def_blocks_checksum() -> None:
    before = parse_blocks("def foo(x=1):\n    return x\n")
    after = parse_blocks("def foo(x=2):\n    return x\n")
    assert _statement_binding(before, "foo").checksum == _statement_binding(after, "foo").checksum
    assert _by_qualname(before, "foo").checksum != _by_qualname(after, "foo").checksum


def test_changing_a_decorators_own_arguments_only_changes_the_statement_blocks_checksum() -> None:
    before = parse_blocks("@app.get('/a')\ndef foo():\n    return 1\n")
    after = parse_blocks("@app.get('/b')\ndef foo():\n    return 1\n")
    assert _statement_binding(before, "foo").checksum != _statement_binding(after, "foo").checksum
    assert _by_qualname(before, "foo").checksum == _by_qualname(after, "foo").checksum


def test_a_decorated_defs_statement_block_is_an_effect() -> None:
    blocks = parse_blocks("@app.get('/a')\ndef foo():\n    return 1\n")
    assert _statement_binding(blocks, "foo").effect is True


def test_a_decorated_defs_own_first_line_is_the_decorators_line() -> None:
    blocks = parse_blocks("@app.get('/a')\ndef foo():\n    return 1\n")
    # decorator on line 1, `def` on line 2
    assert _statement_binding(blocks, "foo").first_line == 1
    assert _by_qualname(blocks, "foo").first_line == 1


def test_a_def_blocks_effect_is_always_false() -> None:
    blocks = parse_blocks("@app.get('/a')\ndef foo():\n    return 1\n")
    assert _by_qualname(blocks, "foo").effect is False


# Duplicate qualnames -- if/else defs
# ------------------------------------------------------------------------


def test_an_if_else_def_produces_two_def_blocks_sharing_a_qualname() -> None:
    blocks = parse_blocks(
        "if True:\n    def dup():\n        return 1\nelse:\n    def dup():\n        return 2\n"
    )
    matches = [b for b in blocks if b.qualname == "dup"]
    assert len(matches) == 2
    assert matches[0].checksum != matches[1].checksum
    # The `if`/`else` itself is one top-level statement block, not two.
    assert sum(1 for b in blocks if b.qualname is None) == 1


# except* (PEP 654) carves the same way as a plain try/except
# ------------------------------------------------------------------------


def test_a_def_nested_under_except_star_gets_its_own_block() -> None:
    blocks = parse_blocks("try:\n    def dup():\n        return 1\nexcept* ValueError:\n    pass\n")
    assert _by_qualname(blocks, "dup") is not None
    assert sum(1 for b in blocks if b.qualname is None) == 1


def test_an_effect_inside_except_star_makes_the_statement_an_effect() -> None:
    blocks = parse_blocks("try:\n    pass\nexcept* ValueError:\n    app.state = 1\n")
    assert len(blocks) == 1
    assert blocks[0].effect is True


# Classes and methods
# ------------------------------------------------------------------------


def test_a_top_level_class_produces_one_statement_block_and_a_def_block_per_method() -> None:
    blocks = parse_blocks(
        "class C:\n    def m(self):\n        return 1\n    def n(self):\n        return 2\n"
    )
    statement = _statement_binding(blocks, "C")
    assert statement.qualname is None
    assert _by_qualname(blocks, "C.m") is not None
    assert _by_qualname(blocks, "C.n") is not None
    # Just the class's own block, plus one per method -- no extra block for either method's name
    # and decorators, which stay embedded in the class's own block.
    assert len(blocks) == 3


def test_changing_a_method_body_only_changes_that_methods_def_block() -> None:
    before = parse_blocks("class C:\n    def m(self):\n        return 1\n")
    after = parse_blocks("class C:\n    def m(self):\n        return 2\n")
    assert _statement_binding(before, "C").checksum == _statement_binding(after, "C").checksum
    assert _by_qualname(before, "C.m").checksum != _by_qualname(after, "C.m").checksum


def test_changing_a_base_class_changes_the_class_statement_block() -> None:
    before = parse_blocks("class C(Base):\n    pass\n")
    after = parse_blocks("class C(OtherBase):\n    pass\n")
    assert _statement_binding(before, "C").checksum != _statement_binding(after, "C").checksum


def test_a_decorated_classs_statement_block_is_an_effect() -> None:
    blocks = parse_blocks("@register\nclass C:\n    pass\n")
    assert _statement_binding(blocks, "C").effect is True


def test_a_methods_decorator_is_visible_in_the_class_statement_block() -> None:
    before = parse_blocks("class C:\n    @a.get('/x')\n    def m(self):\n        return 1\n")
    after = parse_blocks("class C:\n    @a.get('/y')\n    def m(self):\n        return 1\n")
    assert _statement_binding(before, "C").checksum != _statement_binding(after, "C").checksum
    assert _by_qualname(before, "C.m").checksum == _by_qualname(after, "C.m").checksum


def test_a_class_with_a_decorated_method_but_no_class_decorator_is_an_effect() -> None:
    blocks = parse_blocks("class C:\n    @app.get('/x')\n    def m(self):\n        return 1\n")
    assert _statement_binding(blocks, "C").effect is True


def test_a_class_with_an_effect_statement_in_its_body_is_an_effect() -> None:
    blocks = parse_blocks("class C:\n    REGISTRY[C] = 1\n")
    assert _statement_binding(blocks, "C").effect is True


def test_a_plain_class_with_no_decorators_or_effects_is_not_an_effect() -> None:
    blocks = parse_blocks("class C:\n    x = 1\n    def m(self):\n        return 1\n")
    assert _statement_binding(blocks, "C").effect is False


# Nesting
# ------------------------------------------------------------------------


def test_a_nested_def_gets_its_own_qualname_and_block() -> None:
    blocks = parse_blocks("def f():\n    def g():\n        return 1\n    return g\n")
    assert _by_qualname(blocks, "f.<locals>.g") is not None
    # Only `f` gets a standalone statement block; `g`'s name/decorators stay embedded in f's own
    # def block.
    assert sum(1 for b in blocks if b.qualname is None) == 1


def test_editing_a_nested_defs_body_does_not_change_the_outer_def_block() -> None:
    before = parse_blocks("def f():\n    def g():\n        return 1\n    return g\n")
    after = parse_blocks("def f():\n    def g():\n        return 2\n    return g\n")
    assert _by_qualname(before, "f").checksum == _by_qualname(after, "f").checksum
    before_g = _by_qualname(before, "f.<locals>.g")
    after_g = _by_qualname(after, "f.<locals>.g")
    assert before_g.checksum != after_g.checksum


def test_a_method_inside_a_nested_class_gets_a_fully_qualified_name() -> None:
    blocks = parse_blocks(
        "def f():\n    class Inner:\n        def m(self):\n            return 1\n    return Inner\n"
    )
    assert _by_qualname(blocks, "f.<locals>.Inner.m") is not None
    # The nested class has no code object of its own to map onto -- no statement block for it.
    assert sum(1 for b in blocks if b.qualname is None) == 1


def test_editing_a_nested_classs_base_changes_the_enclosing_def_block() -> None:
    before = parse_blocks("def f():\n    class Inner(Base):\n        pass\n    return Inner\n")
    after = parse_blocks("def f():\n    class Inner(OtherBase):\n        pass\n    return Inner\n")
    assert _by_qualname(before, "f").checksum != _by_qualname(after, "f").checksum


# Effects and binds
# ------------------------------------------------------------------------


def test_a_plain_assignment_binds_and_is_not_an_effect() -> None:
    blocks = parse_blocks("x = 1\n")
    statement = _statement_binding(blocks, "x")
    assert statement.effect is False


def test_an_attribute_assignment_is_an_effect_and_binds_nothing() -> None:
    blocks = parse_blocks("app.state = 1\n")
    assert len(blocks) == 1
    assert blocks[0].qualname is None
    assert blocks[0].binds == frozenset()
    assert blocks[0].effect is True
    assert blocks[0].references == frozenset({"app"})


def test_a_subscript_assignment_is_an_effect_referencing_all_three_names() -> None:
    blocks = parse_blocks("REGISTRY[k] = v\n")
    assert len(blocks) == 1
    assert blocks[0].effect is True
    assert blocks[0].references == frozenset({"REGISTRY", "k", "v"})


def test_a_bare_call_expression_is_an_effect() -> None:
    blocks = parse_blocks("app.include_router(r)\n")
    assert len(blocks) == 1
    assert blocks[0].effect is True
    assert blocks[0].references == frozenset({"app", "r"})


def test_import_binds_the_top_level_name() -> None:
    blocks = parse_blocks("import os.path\n")
    assert _statement_binding(blocks, "os") is not None


def test_import_as_binds_the_alias() -> None:
    blocks = parse_blocks("import os.path as op\n")
    assert _statement_binding(blocks, "op") is not None


def test_from_import_as_binds_the_alias() -> None:
    blocks = parse_blocks("from a import b as c\n")
    assert _statement_binding(blocks, "c") is not None


# Whitespace and comments
# ------------------------------------------------------------------------


def test_comments_and_blank_lines_do_not_change_checksums() -> None:
    before = parse_blocks("def foo():\n    return 1\n")
    after = parse_blocks("def foo():  # a comment\n\n\n    return 1\n")
    assert _statement_binding(before, "foo").checksum == _statement_binding(after, "foo").checksum
    assert _by_qualname(before, "foo").checksum == _by_qualname(after, "foo").checksum


# Determinism
# ------------------------------------------------------------------------


def test_parsing_is_deterministic() -> None:
    source = "class C:\n    def m(self):\n        return 1\n\ndef f():\n    return C()\n"
    assert parse_blocks(source) == parse_blocks(source)
