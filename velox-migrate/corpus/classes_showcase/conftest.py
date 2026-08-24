"""The fixtures a class in this suite overrides, and the one written between them.

`blog` is what makes the override cost more than itself: a class that redefines `user` reaches
`blog` too, so a copy of `blog` wired to the class's own `user` is what the conversion has to
write.
"""

import pytest


@pytest.fixture
def user():
    return "root-user"


@pytest.fixture
def blog(user):
    return f"blog:{user}"
