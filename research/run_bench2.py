import sys, ast, os, timeit, marshal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import velox_rewrite as vr

path = "/home/hubert/velox/pytest/testing/test_assertion.py"
srcb = open(path, "rb").read()

print("=== phase breakdown for test_assertion.py (3092 LOC, 217 asserts) ===")
def parse():
    return ast.parse(srcb, filename=path)

t_parse = min(timeit.repeat(parse, number=5, repeat=5)) / 5 * 1e3

tree0 = ast.parse(srcb, filename=path)
def rewrite_only():
    tree = ast.parse(srcb, filename=path)   # must reparse, rewrite mutates
    vr.rewrite_asserts(tree, srcb, path, None)
t_parse_rw = min(timeit.repeat(rewrite_only, number=5, repeat=5)) / 5 * 1e3

def compile_plain():
    compile(tree0, path, "exec", dont_inherit=True)
t_comp_plain = min(timeit.repeat(compile_plain, number=5, repeat=5)) / 5 * 1e3

tree_r = ast.parse(srcb, filename=path)
vr.rewrite_asserts(tree_r, srcb, path, None)
def compile_rw():
    compile(tree_r, path, "exec", dont_inherit=True)
t_comp_rw = min(timeit.repeat(compile_rw, number=5, repeat=5)) / 5 * 1e3

print(f"  ast.parse                : {t_parse:7.2f} ms")
print(f"  AST rewrite pass (alone) : {t_parse_rw - t_parse:7.2f} ms  ({(t_parse_rw-t_parse)/217*1000:.0f} us/assert)")
print(f"  compile (plain tree)     : {t_comp_plain:7.2f} ms")
print(f"  compile (rewritten tree) : {t_comp_rw:7.2f} ms  ({t_comp_rw/t_comp_plain:.2f}x)")
co = compile(tree_r, path, "exec", dont_inherit=True)
blob = marshal.dumps(co)
t_unmarshal = min(timeit.repeat(lambda: marshal.loads(blob), number=50, repeat=5)) / 50 * 1e3
print(f"  marshal.loads (warm pyc) : {t_unmarshal:7.2f} ms   <- cached path")
print(f"  COLD total               : {t_parse_rw + t_comp_rw:7.2f} ms")
print(f"  cold/warm ratio          : {(t_parse_rw + t_comp_rw)/t_unmarshal:7.0f}x")

print()
print("=== meta_path finder overhead: cost paid on EVERY import in the process ===")
# emulate _early_rewrite_bailout for a non-test module name
class FakeState:
    def trace(self, m): pass
from velox_rewrite import AssertionRewritingHook
from _velox_shim import Config

cfg = Config(ini={"python_files": ["test_*.py", "*_test.py"], "enable_assertion_pass_hook": False})
hook = AssertionRewritingHook(cfg)
st = FakeState()
names = ["numpy.core.multiarray", "os.path", "json.decoder", "mypkg.models.user"]
def bail():
    for n in names:
        hook._early_rewrite_bailout(n, st)
t = min(timeit.repeat(bail, number=20000, repeat=5)) / 20000 / len(names) * 1e6
print(f"  _early_rewrite_bailout, non-matching name: {t:.2f} us/import")
def bail_hit():
    hook._early_rewrite_bailout("test_foo", st)
t2 = min(timeit.repeat(bail_hit, number=20000, repeat=5)) / 20000 * 1e6
print(f"  _early_rewrite_bailout, matching name    : {t2:.2f} us/import (then falls through to PathFinder.find_spec)")
