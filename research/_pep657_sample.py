def helper(v):
    return v * 2

def test_plain():
    d = {"a": 1, "b": 2}
    e = {"a": 1, "b": 3}
    assert d == e

def test_call_chain():
    xs = [1, 2, 3]
    assert helper(len(xs)) == 7
