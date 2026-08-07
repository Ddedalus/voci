import sys, ast, time, timeit, os, traceback, textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import velox_rewrite as vr
from _velox_shim import Config

SAMPLE = '''\
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

def test_attr():
    class O: v = 3
    o = O()
    assert o.v == 3
'''

print("=" * 78)
print("1. TRANSFORMED SOURCE")
print("=" * 78)
tree = ast.parse(SAMPLE)
vr.rewrite_asserts(tree, SAMPLE.encode(), "<sample>", None)
print(ast.unparse(tree))

print("=" * 78)
print("2. FAILURE OUTPUT + LINE NUMBERS")
print("=" * 78)
FAIL = '''\
def test_fail():
    d = {"a": 1, "b": 2}
    e = {"a": 1, "b": 3}
    assert d == e


def test_fail_multiline():
    xs = [1, 2, 3]
    assert (
        len(xs)
        == 4
    )
'''
t = ast.parse(FAIL, filename="fake_test.py")
vr.rewrite_asserts(t, FAIL.encode(), "fake_test.py", None)
code = compile(t, "fake_test.py", "exec")
ns = {}
exec(code, ns)
for fn in ("test_fail", "test_fail_multiline"):
    try:
        ns[fn]()
    except AssertionError as e:
        tb = e.__traceback__.tb_next
        print(f"--- {fn}: raised at line {tb.tb_lineno} (source assert lines: 4 / 9-12)")
        print(textwrap.indent(str(e), "    "))

print()
print("=" * 78)
print("3. RUNTIME COST OF *PASSING* ASSERTS")
print("=" * 78)
CASES = {
    "assert x == y (ints)": ("x = 1\ny = 1\n", "assert x == y"),
    "assert x": ("x = True\n", "assert x"),
    "assert a < b and b < c": ("a,b,c = 1,2,3\n", "assert a < b and b < c"),
    "assert d.get('a') == 1": ("d = {'a': 1}\n", "assert d.get('a') == 1"),
    "assert len(xs) == 3": ("xs=[1,2,3]\n", "assert len(xs) == 3"),
}
print(f"{'case':<28}{'plain (ns)':>14}{'rewritten (ns)':>16}{'delta':>12}")
for label, (setup, stmt) in CASES.items():
    body = setup + "def f():\n" + textwrap.indent(setup + stmt, "    ") + "\n"
    # plain
    g1 = {}
    exec(compile(body, "<p>", "exec"), g1)
    # rewritten
    tr = ast.parse(body)
    vr.rewrite_asserts(tr, body.encode(), "<r>", None)
    g2 = {}
    exec(compile(tr, "<r>", "exec"), g2)
    n = 200_000
    t1 = min(timeit.repeat(g1["f"], number=n, repeat=5)) / n * 1e9
    t2 = min(timeit.repeat(g2["f"], number=n, repeat=5)) / n * 1e9
    print(f"{label:<28}{t1:>14.1f}{t2:>16.1f}{t2 - t1:>11.1f}n")

print()
print("=" * 78)
print("4. IMPORT-TIME COST: parse+rewrite+compile vs parse+compile vs marshal.load")
print("=" * 78)
import marshal, glob

targets = [
    "/home/hubert/velox/pytest/testing/test_assertion.py",
    "/home/hubert/velox/pytest/testing/python/metafunc.py",
    "/home/hubert/velox/pytest/testing/test_collection.py",
]
print(f"{'file':<34}{'LOC':>6}{'asserts':>9}{'plain ms':>10}{'rewrite ms':>12}{'ratio':>8}{'marshal ms':>12}")
tot_plain = tot_rw = tot_marshal = 0.0
for path in targets:
    srcb = open(path, "rb").read()
    loc = srcb.count(b"\n")
    nass = sum(isinstance(n, ast.Assert) for n in ast.walk(ast.parse(srcb)))

    def plain():
        compile(ast.parse(srcb, filename=path), path, "exec", dont_inherit=True)

    def rw():
        tree = ast.parse(srcb, filename=path)
        vr.rewrite_asserts(tree, srcb, path, None)
        compile(tree, path, "exec", dont_inherit=True)

    tree = ast.parse(srcb, filename=path)
    vr.rewrite_asserts(tree, srcb, path, None)
    co = compile(tree, path, "exec", dont_inherit=True)
    blob = marshal.dumps(co)

    a = min(timeit.repeat(plain, number=3, repeat=5)) / 3 * 1e3
    b = min(timeit.repeat(rw, number=3, repeat=5)) / 3 * 1e3
    c = min(timeit.repeat(lambda: marshal.loads(blob), number=20, repeat=5)) / 20 * 1e3
    tot_plain += a
    tot_rw += b
    tot_marshal += c
    print(f"{os.path.basename(path):<34}{loc:>6}{nass:>9}{a:>10.2f}{b:>12.2f}{b / a:>8.2f}x{c:>11.2f}")
print(f"{'TOTAL':<34}{'':>6}{'':>9}{tot_plain:>10.2f}{tot_rw:>12.2f}{tot_rw / tot_plain:>8.2f}x{tot_marshal:>11.2f}")
print(f"\npyc blob for last file: {len(blob) / 1024:.0f} KB")

print()
print("=" * 78)
print("5. CODE SIZE BLOWUP (bytecode)")
print("=" * 78)
for path in targets:
    srcb = open(path, "rb").read()
    co_p = compile(ast.parse(srcb, filename=path), path, "exec", dont_inherit=True)
    tree = ast.parse(srcb, filename=path)
    vr.rewrite_asserts(tree, srcb, path, None)
    co_r = compile(tree, path, "exec", dont_inherit=True)
    lp, lr = len(marshal.dumps(co_p)), len(marshal.dumps(co_r))
    print(f"{os.path.basename(path):<34}{lp / 1024:>8.0f} KB -> {lr / 1024:>8.0f} KB  ({lr / lp:.2f}x)")
