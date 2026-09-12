"""Part 4: repeat the precision measurement on oss/httpx."""
import statistics
import time
from collections import Counter
from pathlib import Path

from namedeps import BlockKey, build_analyzer

HX = Path("/home/hubert/voci/oss/httpx")


def is_first_party(name: str) -> bool:
    return name == "httpx" or name.startswith("httpx.")


def main():
    t0 = time.time()
    pkg_only = build_analyzer((HX / "httpx", "httpx"))
    pkg_only_time = time.time() - t0

    t0 = time.time()
    a = build_analyzer((HX / "httpx", "httpx"), (HX / "tests", "tests"))
    build_time = time.time() - t0

    test_modules = sorted(
        m for m in a.modules
        if (m.startswith("tests.") or m == "tests")
        and a.modules[m].name.rsplit(".", 1)[-1].startswith("test_")
    )
    total = len(test_modules)
    print(f"httpx/-only build time: {pkg_only_time:.3f}s")
    print(f"full tree build time: {build_time:.3f}s")
    print(f"test modules considered: {total}")

    fp_blocks: list[BlockKey] = []
    for m in a.modules.values():
        if is_first_party(m.name):
            fp_blocks.extend(BlockKey(m.name, b.index) for b in m.blocks)
    print(f"httpx/ top-level statements: {len(fp_blocks)}")

    file_level_count: Counter[BlockKey] = Counter()
    name_level_count: Counter[BlockKey] = Counter()

    t0 = time.time()
    for tm in test_modules:
        reached_modules = a.static_import_closure_blocks(tm)
        for rm in reached_modules:
            if is_first_party(rm):
                mod = a.modules[rm]
                for b in mod.blocks:
                    file_level_count[BlockKey(rm, b.index)] += 1

        refs = a.file_refs(tm)
        closure_result = a.closure(tm, refs)
        expanded = a.expand(closure_result)
        for bk in expanded:
            if is_first_party(bk.module):
                name_level_count[bk] += 1
    measure_time = time.time() - t0
    print(f"measurement time over {total} test modules: {measure_time:.3f}s")

    def stats(counter: Counter[BlockKey], label: str):
        fracs = [counter.get(bk, 0) / total for bk in fp_blocks]
        median = statistics.median(fracs)
        mean = statistics.mean(fracs)
        over50 = sum(1 for f in fracs if f > 0.5) / len(fracs)
        under10 = sum(1 for f in fracs if f < 0.10) / len(fracs)
        zero = sum(1 for f in fracs if f == 0.0) / len(fracs)
        print(f"-- {label} --")
        print(f"  median={median:.3%} mean={mean:.3%} >50%={over50:.1%} <10%={under10:.1%} =0%={zero:.1%}")

    stats(file_level_count, "file-level rule")
    stats(name_level_count, "name-level rule")

    fallbacks = {m.name: m.fallback_reasons for m in a.modules.values() if m.whole_module_fallback and is_first_party(m.name)}
    print("fallback httpx/ modules:", fallbacks)
    print("gaps:", a.gaps)


if __name__ == "__main__":
    main()
