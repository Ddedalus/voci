#!/usr/bin/env python3
"""Assert the assertion-rewrite cold/warm ratio stays within budget (spec/07 §5).

velox is benchmarked cold in CI containers, and the whole cold-start argument rests on two
measured numbers (R§2): rewriting costs ~4.6x a plain compile, and loading the cached pyc is
~154x faster than that. If the cache ever stops being load-bearing — a botched cache key, a
probe that silently fails, a codegen change that inflates the AST pass — every run quietly
pays the cold price and no test fails.

    uv run python scripts/bench_cold_start.py

Exits non-zero when a budget is missed. Budgets are deliberately loose: this catches a
regression of kind, not of degree, and must not go red because CI was noisy.
"""

from __future__ import annotations

import argparse
import marshal
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from velox._vendor.assertion import rewrite as vendored  # noqa: E402
from velox._vendor.assertion._shim import Config  # noqa: E402

#: Warm load must be at least this many times faster than a cold rewrite. Measured at ~154x;
#: an order of magnitude of headroom, because the claim being defended is "the cache is
#: load-bearing", not a particular number.
MIN_WARM_SPEEDUP = 20.0

#: Cold rewrite versus a plain `compile()` of the same source. Measured at ~4.6x on a real
#: test file, so it depends on how assert-dense the input is — hence the realistic generator
#: below, and the generous ceiling.
MAX_COLD_PENALTY = 12.0

#: Cold rewrite cost per assert, in seconds. Measured at ~164 us. Unlike the ratio above this
#: is density-independent, which makes it the sharper of the two budgets.
MAX_COLD_PER_ASSERT = 600e-6

REPEATS = 5


def _sample(source: str, path: Path) -> tuple[float, float, float]:
    """One (plain compile, cold rewrite, warm load) triple, in seconds."""
    text = path.read_text()

    start = time.perf_counter()
    compile(text, str(path), "exec")
    plain = time.perf_counter() - start

    start = time.perf_counter()
    _, code = vendored._rewrite_test(path, Config())
    cold = time.perf_counter() - start

    blob = marshal.dumps(code)
    start = time.perf_counter()
    marshal.loads(blob)
    warm = time.perf_counter() - start

    return plain, cold, warm


def _generate(path: Path, asserts: int) -> str:
    """A module shaped like a real test file, because the cold ratio depends on that shape.

    R§2's 4.6x came from `pytest/testing/test_assertion.py` — 3092 lines, 217 asserts, so an
    assert roughly every 14 lines, and mostly simple ones. A module that is nothing but
    compound asserts measures ~30x instead, which says more about the generator than about the
    rewriter. This mirrors upstream's density and assert complexity.
    """
    body = [
        "import os\n",
        "import sys\n",
        "from dataclasses import dataclass\n",
        "\n\n@dataclass\nclass Record:\n    name: str\n    value: int\n",
    ]
    for i in range(asserts):
        body.append(
            f"\n\ndef test_case_{i}():\n"
            f'    """Docstring for case {i}, so the module is not pure assert."""\n'
            f"    record = Record(name={i!r}, value={i})\n"
            f"    scaled = record.value * 2\n"
            f"    label = record.name.upper()\n"
            f"    payload = {{'label': label, 'scaled': scaled}}\n"
            f"    if scaled > 1000:\n"
            f"        payload['big'] = True\n"
            f"    for key in sorted(payload):\n"
            f"        _ = payload[key]\n"
            f"    expected = {{'label': label, 'scaled': scaled}}\n"
            f"    assert payload == expected\n"
        )
    source = "".join(body)
    path.write_text(source)
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asserts", type=int, default=200, help="asserts in the sample module")
    parser.add_argument("--repeats", type=int, default=REPEATS)
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="velox-bench-"))
    try:
        path = tmp / "test_generated.py"
        source = _generate(path, args.asserts)

        samples = [_sample(source, path) for _ in range(args.repeats)]
        plain = statistics.median(s[0] for s in samples)
        cold = statistics.median(s[1] for s in samples)
        warm = statistics.median(s[2] for s in samples)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    cold_penalty = cold / plain
    warm_speedup = cold / warm
    cold_per_assert = cold / args.asserts

    lines = len(source.splitlines())
    print(f"module:        {lines} lines, {args.asserts} asserts, median of {args.repeats} runs")
    print(f"plain compile: {plain * 1000:8.2f} ms")
    print(
        f"cold rewrite:  {cold * 1000:8.2f} ms   ({cold_penalty:.1f}x plain compile, "
        f"{cold_per_assert * 1e6:.0f} us/assert)"
    )
    print(f"warm load:     {warm * 1000:8.2f} ms   ({warm_speedup:.0f}x faster than cold)")
    print()

    failures = []
    if cold_penalty > MAX_COLD_PENALTY:
        failures.append(
            f"cold rewrite is {cold_penalty:.1f}x a plain compile, budget is {MAX_COLD_PENALTY}x"
        )
    if cold_per_assert > MAX_COLD_PER_ASSERT:
        failures.append(
            f"cold rewrite costs {cold_per_assert * 1e6:.0f} us/assert, budget is "
            f"{MAX_COLD_PER_ASSERT * 1e6:.0f} us"
        )
    if warm_speedup < MIN_WARM_SPEEDUP:
        failures.append(
            f"warm load is only {warm_speedup:.0f}x faster than cold, budget is "
            f"{MIN_WARM_SPEEDUP:.0f}x — the pyc cache has stopped being load-bearing"
        )

    if failures:
        for line in failures:
            print(f"FAIL: {line}", file=sys.stderr)
        return 1

    print("within budget")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
