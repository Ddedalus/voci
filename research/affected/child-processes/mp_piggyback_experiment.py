"""Instead of env-var injection, piggyback the test id as a plain attribute
on the multiprocessing.Process object -- it travels with the pickled process
object through spawn/forkserver, and is just already-there in memory for the
"fork" method. This should survive forkserver reuse across "tests" (unlike
env vars, which freeze at the forkserver's own one-time launch).
"""
import multiprocessing
import multiprocessing.process
import os
import sys
import threading
import time

sys.path.insert(0, "/tmp/child-trace/scripts")
import fp_module  # noqa: E402

_OriginalBootstrap = multiprocessing.process.BaseProcess._bootstrap
_OriginalStart = multiprocessing.process.BaseProcess.start


def _patched_start(self, *a, **kw):
    # Snapshot "current test" at the moment start() is called -- this is
    # the piggyback. In a real voci this would read the collector ContextVar.
    self._voci_test_id = os.environ.get("VOCI_CURRENT_TEST", "<unset>")
    return _OriginalStart(self, *a, **kw)


def _patched_bootstrap(self, *a, **kw):
    tid = getattr(self, "_voci_test_id", "<no-attr>")
    with open(f"/tmp/child-trace/mp_piggyback_{os.getpid()}.txt", "w") as f:
        f.write(f"pid={os.getpid()} piggybacked_test_id={tid}\n")
    return _OriginalBootstrap(self, *a, **kw)


multiprocessing.process.BaseProcess.start = _patched_start
multiprocessing.process.BaseProcess._bootstrap = _patched_bootstrap


def target():
    fp_module.do_work("mp-piggyback-child")


def run_case(method_name, test_label):
    os.environ["VOCI_CURRENT_TEST"] = test_label
    ctx = multiprocessing.get_context(method_name)
    p = ctx.Process(target=target)
    p.start()
    p.join()


if __name__ == "__main__":
    for f in __import__("glob").glob("/tmp/child-trace/mp_piggyback_*.txt"):
        os.remove(f)

    print("--- forkserver reused across 3 'tests', env changes between calls ---")
    run_case("forkserver", "fs-test-1")
    run_case("forkserver", "fs-test-2")
    run_case("forkserver", "fs-test-3")

    print("--- fork, 2 'tests' ---")
    run_case("fork", "fork-test-1")
    run_case("fork", "fork-test-2")

    print("--- spawn, 2 'tests' ---")
    run_case("spawn", "spawn-test-1")
    run_case("spawn", "spawn-test-2")

    time.sleep(0.3)
    print("\n--- results ---")
    for f in sorted(__import__("glob").glob("/tmp/child-trace/mp_piggyback_*.txt")):
        print(open(f).read().strip())
