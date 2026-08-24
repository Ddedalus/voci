"""Fixtures written inside a test class, in the shapes lifting them out has to answer.

Contributes the class-overrides-a-conftest-fixture case (`TestOverriding.user`, with `blog` and
`digest` downstream of it, so a copy is wired to another copy), the two-classes-bind-the-same-name
case (`base`, in `TestSiblings` and again in `TestSameName`), the one-class-fixture-depends-on-
another case (`derived`, written above the `base` it names, so source order alone would place it
wrong), the fixture-reads-a-class-attribute case (`through_self`, whose `self` only ever means the
class) and the nested-def-declares-its-own-self case (`nested`, whose inner `self` is not the
factory's and must survive the move untouched).
"""

import pytest


class TestOverriding:
    @pytest.fixture
    def user(self):
        return "class-user"

    def test_the_override_wins(self, user):
        assert user == "class-user"

    def test_what_is_written_between_follows_it(self, blog):
        assert blog == "blog:class-user"

    def test_the_whole_chain_above_it_follows_it(self, digest):
        assert digest == "digest:blog:class-user"


class TestSiblings:
    STAMP = "stamped"

    @pytest.fixture
    def derived(self, base):
        return f"{base}+derived"

    @pytest.fixture
    def base(self):
        return "sibling-base"

    @pytest.fixture
    def through_self(self):
        return self.STAMP

    @pytest.fixture
    def nested(self):
        stamp = self.STAMP

        class Built:
            marker = stamp

            def label(self):
                return self.marker

        return Built().label()

    def test_one_fixture_reaches_another(self, derived):
        assert derived == "sibling-base+derived"

    def test_a_class_attribute_is_still_readable(self, through_self):
        assert through_self == "stamped"

    def test_a_self_a_nested_def_declares_is_its_own(self, nested):
        assert nested == "stamped"


class TestSameName:
    @pytest.fixture
    def base(self):
        return "other-base"

    def test_each_class_keeps_its_own(self, base):
        assert base == "other-base"


def test_a_module_level_test_still_resolves_the_conftest(blog):
    assert blog == "blog:root-user"
