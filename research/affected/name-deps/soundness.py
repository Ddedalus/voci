"""Soundness harness: for each case, apply an edit to a file in proj/, rebuild
the analyzer before/after, find which top-level block's hash changed, and
check whether that block is in the dependency closure of the relevant test
function(s)."""
import ast
import copy
import hashlib
from pathlib import Path

from namedeps import Analyzer, Block, build_analyzer, extract_full_refs

ROOT = Path("/tmp/namedeps/proj")
PKG = ROOT / "pkg"
TESTS = ROOT / "tests"


def mask_for_hash(node: ast.AST) -> ast.AST:
    node = copy.deepcopy(node)

    def mask(n: ast.AST) -> None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n.body = [ast.Pass()]
            return
        for _, value in ast.iter_fields(n):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        mask(item)
            elif isinstance(value, ast.AST):
                mask(value)

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        node.body = [ast.Pass()]
    else:
        mask(node)
    return node


def block_hash(node: ast.AST) -> str:
    masked = mask_for_hash(node)
    return hashlib.blake2b(ast.dump(masked, include_attributes=False).encode(), digest_size=8).hexdigest()


def build() -> Analyzer:
    return build_analyzer((PKG, "pkg"), (TESTS, "tests"))


def test_function_node(analyzer: Analyzer, test_name: str) -> ast.AST:
    mod = analyzer.modules["tests.test_cases"]
    for b in mod.blocks:
        if b.kind == "FunctionDef" and b.node.name == test_name:
            return b.node
    raise KeyError(test_name)


def closure_for_test(analyzer: Analyzer, test_name: str) -> set:
    node = test_function_node(analyzer, test_name)
    refs = extract_full_refs(node)
    result = analyzer.closure("tests.test_cases", refs)
    return analyzer.expand(result)


def diff_blocks(before: Analyzer, after: Analyzer, module: str) -> list[int]:
    b_blocks = before.modules[module].blocks
    a_blocks = after.modules[module].blocks
    changed = []
    for i in range(min(len(b_blocks), len(a_blocks))):
        if block_hash(b_blocks[i].node) != block_hash(a_blocks[i].node):
            changed.append(i)
    if len(a_blocks) > len(b_blocks):
        changed.extend(range(len(b_blocks), len(a_blocks)))
    return changed


def run_case(label, file, old, new, module, tests_expected_to_catch, note=""):
    path = ROOT / file
    original = path.read_text()
    assert old in original, f"{label}: old string not found in {file}"
    path.write_text(original.replace(old, new, 1))
    try:
        before = build()  # rebuild each time so "before" reflects any earlier reverted state
    finally:
        pass
    # actually we need a clean "before" snapshot pre-edit; redo properly below
    path.write_text(original)
    before = build()
    path.write_text(original.replace(old, new, 1))
    after = build()
    path.write_text(original)  # revert

    changed = diff_blocks(before, after, module)
    caught_by = []
    missed_by = []
    for t in tests_expected_to_catch:
        closure = closure_for_test(after, t)
        hit = any(k.module == module and k.index in changed for k in closure)
        (caught_by if hit else missed_by).append(t)
    status = "CAUGHT" if not missed_by else ("PARTIAL" if caught_by else "MISSED")
    print(f"[{status}] {label}")
    print(f"    changed blocks in {module}: {changed}")
    print(f"    caught by: {caught_by}  missed by: {missed_by}")
    if note:
        print(f"    note: {note}")
    print()
    return status


if __name__ == "__main__":
    run_case(
        "1a. module constant read via `from config import TIMEOUT`",
        "pkg/config.py", "TIMEOUT = 30", "TIMEOUT = 999",
        "pkg.config", ["test_const_via_from_import"],
    )
    run_case(
        "1b. constant computed from another constant (edit upstream TIMEOUT, "
        "read via `config.TOTAL_BUDGET`)",
        "pkg/config.py", "TIMEOUT = 30", "TIMEOUT = 999",
        "pkg.config", ["test_const_via_module_attr"],
        note="checks transitive closure over a block's own references",
    )
    run_case(
        "1c. constant computed from another (edit RETRIES instead)",
        "pkg/config.py", "RETRIES = 3", "RETRIES = 9",
        "pkg.config", ["test_const_via_module_attr"],
    )
    run_case(
        "2a. type alias (Annotated) used in a pydantic model field",
        "pkg/types_mod.py", "Field(gt=0)", "Field(gt=5)",
        "pkg.types_mod", ["test_pydantic_model"],
    )
    run_case(
        "2b. PEP 695 `type` alias used in a pydantic model field",
        "pkg/types_mod.py", "Field(ge=0, le=100)", "Field(ge=0, le=90)",
        "pkg.types_mod", ["test_pydantic_model"],
    )
    run_case(
        "3a. pydantic model: add a field to a nested model (Address)",
        "pkg/models.py", 'zip_code: str = Field(min_length=3)',
        'zip_code: str = Field(min_length=3)\n    country: str = "US"',
        "pkg.models", ["test_pydantic_model"],
    )
    run_case(
        "3b. pydantic model: change a Field(...) constraint",
        "pkg/models.py", "min_length=3", "min_length=4",
        "pkg.models", ["test_pydantic_model"],
    )
    run_case(
        "3c. pydantic model: change model_config",
        "pkg/models.py", "ConfigDict(frozen=True)", "ConfigDict(frozen=False)",
        "pkg.models", ["test_pydantic_model"],
    )
    run_case(
        "3d. pydantic model: forward reference by string (edit Manager, "
        "referenced only as `\"Manager | None\"`)",
        "pkg/models.py", "level: int = 1", "level: int = 2",
        "pkg.models", ["test_pydantic_model"],
    )
    run_case(
        "3e. pydantic model used only as a handler parameter annotation "
        "(test never constructs it directly)",
        "pkg/models.py", "zip_code: str = Field(min_length=3)",
        "zip_code: str = Field(min_length=5)",
        "pkg.models", ["test_app_handler_param_model"],
        note="caught via handle_user's *signature* reference to User, a def-block "
        "concern independent of whether pydantic-core traces any Python frames",
    )
    run_case(
        "4a. dataclass field default change",
        "pkg/dataclasses_mod.py", 'name: str = "widget"', 'name: str = "gadget"',
        "pkg.dataclasses_mod", ["test_dataclass_widget"],
    )
    run_case(
        "4b. Enum member added",
        "pkg/dataclasses_mod.py", 'GREEN = "green"', 'GREEN = "green"\n    BLUE = "blue"',
        "pkg.dataclasses_mod", ["test_dataclass_widget"],
        note="Color isn't referenced by name in the test's closure except via "
        "Widget.color: Color -- confirms the class-attribute-type path",
    )
    run_case(
        "4c. base class change",
        "pkg/dataclasses_mod.py", 'kind = "base"', 'kind = "base-v2"',
        "pkg.dataclasses_mod", ["test_dataclass_widget"],
    )
    run_case(
        "5. re-export hub: edit UNRELATED statement in pkg/routing.py",
        "pkg/routing.py", "return 1", "return 2",
        "pkg.routing", ["test_reexport_hub_router"],
    )
    print("(expect the unrelated-statement case above to be MISSED -- that's correct)\n")
    run_case(
        "5b. re-export hub: edit Router's class body",
        "pkg/routing.py", 'prefix: str = "/"', 'prefix: str = "//"',
        "pkg.routing", ["test_reexport_hub_router"],
    )
    run_case(
        "6a. registry: change an existing registration's key string",
        "pkg/registry.py", '@register("greet")', '@register("greetings")',
        "pkg.registry", ["test_registry_greet"],
        note="requires the decorator-factory widening heuristic (register() mutates "
        "REGISTRY via closure) -- without it this is MISSED, see report",
    )
    run_case(
        "6b. registry: add a brand-new registration",
        "pkg/registry.py", 'def call_registered(name):',
        '@register("shout")\ndef shout_handler():\n    return "SHOUT"\n\n\n'
        'def call_registered(name):',
        "pkg.registry", ["test_registry_greet"],
        note="a test that only ever dispatches \"greet\" arguably shouldn't need to "
        "rerun on an unrelated new key -- but REGISTRY is a single mutable object "
        "the closure can't subdivide by key, so this is conservative-but-sound "
        "over-selection, same as the file-level rule would give",
    )
    run_case(
        "7. brand-new undecorated function added (should re-run nothing)",
        "pkg/app_mod.py", "def undecorated_helper():",
        "def undecorated_helper():\n    pass\ndef brand_new_fn():",
        "pkg.app_mod", [],
        note="no test references brand_new_fn; verifying it shows up as a new "
        "block that no closure touches",
    )
    run_case(
        "8. new decorated route added, referencing `app` (should re-run tests "
        "using the app)",
        "pkg/extra_routes.py", 'def extra_handler():\n    return "extra"',
        'def extra_handler():\n    return "extra"\n\n\n@app.get("/new")\ndef new_handler():\n    return "new"',
        "pkg.extra_routes", ["test_extra_route_registered"],
        note="new_handler's block isn't referenced by name anywhere; caught only "
        "via cross-module effect-fold onto app's binding block",
    )
    run_case(
        "9. importlib.import_module(f'pkg.plugins.{name}') -- expected gap",
        "pkg/plugins/foo.py", 'VALUE = "foo-plugin"', 'VALUE = "foo-plugin-v2"',
        "pkg.plugins.foo", ["test_dynamic_plugin_load"],
        note="expected MISSED: the module name is built from an f-string, "
        "invisible to static string/attr resolution",
    )
    run_case(
        "10. if TYPE_CHECKING / try-except conditional def / __all__ / del",
        "pkg/typecheck_mod.py", 'import json as _fast_json', 'import copy as _fast_json',
        "pkg.typecheck_mod", ["test_typecheck_module"],
    )
