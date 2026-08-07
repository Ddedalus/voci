import sys, types, os, time, ast, timeit

SRC = "/home/hubert/velox/pytest/src"
sys.path.insert(0, SRC)
# stub out _pytest._version (repo has no built version file)
m = types.ModuleType("_pytest._version")
m.version = "9.9.9"
m.__version__ = "9.9.9"
sys.modules["_pytest._version"] = m

# stub pygments (hard dep of _pytest._io.terminalwriter)
for name in ("pygments", "pygments.formatters", "pygments.formatters.terminal",
             "pygments.lexer", "pygments.lexers", "pygments.lexers.diff",
             "pygments.lexers.python", "pygments.util"):
    mod = types.ModuleType(name)
    sys.modules.setdefault(name, mod)
sys.modules["pygments"].highlight = lambda *a, **k: a[0]
sys.modules["pygments.formatters.terminal"].TerminalFormatter = object
sys.modules["pygments.lexer"].Lexer = object
sys.modules["pygments.lexers.diff"].DiffLexer = object
sys.modules["pygments.lexers.python"].PythonLexer = object

pl = types.ModuleType("pluggy")
pl.__file__ = "/nonexistent/pluggy/__init__.py"
sys.modules["pluggy"] = pl

from _pytest.assertion.rewrite import AssertionRewriter, rewrite_asserts

SAMPLE = '''
def test_simple():
    x = 1
    y = 1
    assert x == y

def test_call():
    d = {"a": 1}
    assert d.get("a") == 1, "boom"

def test_bool():
    a, b, c = 1, 2, 3
    assert a < b and b < c

def test_in():
    assert "a" in "abc"
'''

tree = ast.parse(SAMPLE.encode())
rewrite_asserts(tree, SAMPLE.encode(), "<sample>", None)
print("=" * 70)
print("REWRITTEN SOURCE (ast.unparse)")
print("=" * 70)
print(ast.unparse(tree))
