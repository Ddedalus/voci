"""A module handed a copied fixture without naming it, through a module-level `usefixtures`.

The declaration this becomes covers tests inside the overriding directory, so the object it names
is that directory's copy rather than the definition the copy was made from.
"""

import pytest

pytestmark = pytest.mark.usefixtures("client")


def test_declared(report):
    assert report == "report:engine:sqlite://integration"
