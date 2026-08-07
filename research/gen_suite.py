"""Regenerate the synthetic suites used for the collection/run benchmarks in RESEARCH.md.

Creates, under --out (default: ./suite and ./psuite next to this file):
  suite/   200 files x 25 trivial tests = 5000 tests, 20 package dirs,
           each dir with a conftest defining a `local_fx` fixture, empty root conftest.
  psuite/  a single file with one test parametrized over range(5000).

Benchmarks reproduced against these (see RESEARCH.md §3 for results):
  pytest --co -q suite            # collection only  (~0.87s at time of research)
  pytest -q suite                 # full run          (~3.95s)
  pytest -q psuite                # parametrize expansion is ~cheap (~24us/item)
  python -m cProfile -s cumulative -m pytest --co -q suite
"""

import argparse
import pathlib

N_PKGS = 20
FILES_PER_PKG = 10
TESTS_PER_FILE = 25

PKG_CONFTEST = """\
import pytest
@pytest.fixture
def local_fx():
    return 1
"""

PSUITE_TEST = """\
import pytest
@pytest.mark.parametrize("a", list(range(5000)))
def test_p(a):
    assert a>=0
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path(__file__).parent)
    args = ap.parse_args()

    suite = args.out / "suite"
    suite.mkdir(parents=True, exist_ok=True)
    (suite / "conftest.py").write_text("")
    for p in range(N_PKGS):
        pkg = suite / f"pkg{p}"
        pkg.mkdir(exist_ok=True)
        (pkg / "conftest.py").write_text(PKG_CONFTEST)
        for f in range(FILES_PER_PKG):
            body = ["import pytest"]
            for t in range(TESTS_PER_FILE):
                body.append(f"def test_{t}(local_fx):")
                body.append("    assert local_fx == 1")
            (pkg / f"test_mod{p}_{f}.py").write_text("\n".join(body) + "\n")

    psuite = args.out / "psuite"
    psuite.mkdir(parents=True, exist_ok=True)
    (psuite / "test_p.py").write_text(PSUITE_TEST)

    print(f"wrote {N_PKGS * FILES_PER_PKG} files / {N_PKGS * FILES_PER_PKG * TESTS_PER_FILE} tests to {suite}")
    print(f"wrote parametrized suite to {psuite}")


if __name__ == "__main__":
    main()
