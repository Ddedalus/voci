"""A directory that redefines `teardowns`, so everything reaching it is copied for this subtree.

Contributes the specialized-copy-of-a-rewritten-body case: `report` is written at the root, reads
a fixture by name and registers a finalizer, and this override makes the subtree need its own copy
of it — which has to be rewritten the same way the original is.
"""

import pytest


@pytest.fixture(scope="session")
def teardowns():
    return ["sub"]
