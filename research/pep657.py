import sys, traceback, textwrap, os

SRC = '''\
def helper(v):
    return v * 2

def test_plain():
    d = {"a": 1, "b": 2}
    e = {"a": 1, "b": 3}
    assert d == e

def test_call_chain():
    xs = [1, 2, 3]
    assert helper(len(xs)) == 7
'''
p = "/tmp/claude-1000/-home-hubert-velox/de5d143f-2c6a-4bf9-8de7-49c8aaed567e/scratchpad/_pep657_sample.py"
open(p, "w").write(SRC)
sys.path.insert(0, os.path.dirname(p))
import _pep657_sample as m

print("python", sys.version.split()[0])
for fn in ("test_plain", "test_call_chain"):
    try:
        getattr(m, fn)()
    except AssertionError:
        print(f"--- {fn} ---")
        traceback.print_exc()
    print()
