"""Part 3: precision measurement on oss/fastapi.

For every top-level statement in every fastapi/ module, compute the fraction
of test files whose dependency closure includes it, under (a) the file-level
rule (module block reached via the static import closure of the test file)
and (b) the name-level rule (approximated: seed with everything the whole
test file references, resolve through the analyzer)."""
import statistics
import time
from collections import Counter
from pathlib import Path

from namedeps import BlockKey, build_analyzer

FA = Path("/home/hubert/voci/oss/fastapi")


def is_fastapi(name: str) -> bool:
    return name == "fastapi" or name.startswith("fastapi.")


def build_full():
    t0 = time.time()
    a = build_analyzer((FA / "fastapi", "fastapi"), (FA / "tests", "tests"), (FA / "docs_src", "docs_src"))
    return a, time.time() - t0


def fastapi_only_timing():
    t0 = time.time()
    build_analyzer((FA / "fastapi", "fastapi"))
    return time.time() - t0


def main():
    fa_time = fastapi_only_timing()
    a, build_time = build_full()

    test_modules = sorted(
        m for m in a.modules
        if (m.startswith("tests.") or m == "tests")
        and a.modules[m].name.rsplit(".", 1)[-1].startswith("test_")
    )
    total = len(test_modules)
    print(f"fastapi/-only build time: {fa_time:.3f}s")
    print(f"full tree (fastapi+tests+docs_src) build time: {build_time:.3f}s")
    print(f"test files considered: {total}")

    fastapi_blocks: list[BlockKey] = []
    for m in a.modules.values():
        if is_fastapi(m.name):
            fastapi_blocks.extend(BlockKey(m.name, b.index) for b in m.blocks)
    print(f"fastapi/ top-level statements: {len(fastapi_blocks)}")

    file_level_count: Counter[BlockKey] = Counter()
    name_level_count: Counter[BlockKey] = Counter()
    routing_reach_count = 0

    t0 = time.time()
    for tm in test_modules:
        reached_modules = a.static_import_closure_blocks(tm)
        if "fastapi.routing" in reached_modules:
            routing_reach_count += 1
        for rm in reached_modules:
            if is_fastapi(rm):
                mod = a.modules[rm]
                for b in mod.blocks:
                    file_level_count[BlockKey(rm, b.index)] += 1

        refs = a.file_refs(tm)
        closure_result = a.closure(tm, refs)
        expanded = a.expand(closure_result)
        for bk in expanded:
            if is_fastapi(bk.module):
                name_level_count[bk] += 1
    measure_time = time.time() - t0
    print(f"measurement time over {total} test files: {measure_time:.3f}s")

    def stats(counter: Counter[BlockKey], label: str):
        fracs = [counter.get(bk, 0) / total for bk in fastapi_blocks]
        median = statistics.median(fracs)
        mean = statistics.mean(fracs)
        over50 = sum(1 for f in fracs if f > 0.5) / len(fracs)
        under10 = sum(1 for f in fracs if f < 0.10) / len(fracs)
        zero = sum(1 for f in fracs if f == 0.0) / len(fracs)
        print(f"-- {label} --")
        print(f"  median={median:.3%} mean={mean:.3%} >50%={over50:.1%} <10%={under10:.1%} =0%={zero:.1%}")
        return fracs

    stats(file_level_count, "file-level rule")
    stats(name_level_count, "name-level rule")

    print()
    print(f"fraction of test files with fastapi.routing in static import closure: "
          f"{routing_reach_count}/{total} = {routing_reach_count/total:.1%}")

    # constant in fastapi/_compat/shared.py
    shared = a.modules["fastapi._compat.shared"]
    const_idx = next(i for i, b in enumerate(shared.blocks) if "PYDANTIC_VERSION_MINOR_TUPLE" in b.binds)
    const_key = BlockKey("fastapi._compat.shared", const_idx)
    fl = file_level_count.get(const_key, 0) / total
    nl = name_level_count.get(const_key, 0) / total
    print(f"fastapi._compat.shared PYDANTIC_VERSION_MINOR_TUPLE (block {const_idx}): "
          f"file-level={fl:.1%} name-level={nl:.1%}")

    # pydantic model field in openapi/models.py -- pick Contact (small, low fan-in
    # expected) and Schema (large, likely high fan-in)
    models_mod = a.modules["fastapi.openapi.models"]
    for cls_name in ("Contact", "Schema", "Info"):
        idx = next(i for i, b in enumerate(models_mod.blocks) if cls_name in b.binds)
        key = BlockKey("fastapi.openapi.models", idx)
        fl = file_level_count.get(key, 0) / total
        nl = name_level_count.get(key, 0) / total
        print(f"fastapi.openapi.models.{cls_name} (block {idx}): file-level={fl:.1%} name-level={nl:.1%}")

    return a, file_level_count, name_level_count, test_modules, total


if __name__ == "__main__":
    main()
