"""The fixtures a class in this suite overrides, and the one written between them.

`blog` and `digest` are what make the override cost more than itself: a class that redefines
`user` reaches both, so the conversion has to write a copy of each, wired to the class's own
`user` and then to each other rather than to what they were written against.
"""

import pytest


@pytest.fixture
def user():
    return "root-user"


@pytest.fixture
def blog(user):
    return f"blog:{user}"


@pytest.fixture
def digest(blog):
    return f"digest:{blog}"
