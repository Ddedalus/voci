"""A test that must never be collected: this directory is named in `norecursedirs`.

Contributes the norecursedirs case. If the setting stops being honoured the suite fails loudly
here, rather than quietly widening.
"""


def test_never_collected():
    raise AssertionError("scratch/ is excluded from collection")
