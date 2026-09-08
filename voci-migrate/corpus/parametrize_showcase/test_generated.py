"""Cases a hook built while pytest collected, frozen into the parametrize that lists them.

Contributes a module's own `pytest_generate_tests` alongside the conftest's: one axis over two
argnames carrying the ids the hook chose, and one axis the conftest hook supplies to a test that
names nothing else.
"""


def pytest_generate_tests(metafunc):
    if "width" in metafunc.fixturenames:
        metafunc.parametrize("width,height", [(2, 3), (5, 8)], ids=["small", "large"])


def test_area(width, height):
    assert width * height in (6, 40)


def test_letters(letter):
    assert letter in ("a", "b")
