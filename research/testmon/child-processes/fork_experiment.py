"""Run with VOCI_AFFECTED_DIR already set in the environment at interpreter
startup (mimicking a real voci session, where the tracer activates once for
the whole test-runner process). Exercises multiprocessing's three start
methods plus a raw os.fork(), all from a single already-instrumented parent.
"""
import multiprocessing
import os
import sys
import time

sys.path.insert(0, "/tmp/child-trace/scripts")
import fp_module  # noqa: E402


def _mp_target(tag):
    fp_module.do_work(tag)


def run_case(method_name, use_ctx=True):
    print(f"\n=== {method_name} ===")
    os.environ["VOCI_TEST_ID"] = f"test-{method_name}"
    if use_ctx:
        ctx = multiprocessing.get_context(method_name)
        p = ctx.Process(target=_mp_target, args=(method_name,))
    else:
        p = multiprocessing.Process(target=_mp_target, args=(method_name,))
    p.start()
    p.join(timeout=10)
    print(f"exitcode={p.exitcode}")


def run_raw_fork():
    print("\n=== raw os.fork ===")
    os.environ["VOCI_TEST_ID"] = "test-raw-fork"
    fp_module.do_work("before-fork")
    pid = os.fork()
    if pid == 0:
        os.environ["VOCI_TEST_ID"] = "test-raw-fork-CHILD-set-after-fork"
        fp_module.do_work("forked-child")
        time.sleep(0.05)
        os._exit(0)
    else:
        os.waitpid(pid, 0)


if __name__ == "__main__":
    print("parent pid", os.getpid(), "VOCI_AFFECTED_DIR=", os.environ.get("VOCI_AFFECTED_DIR"))
    run_case("spawn")
    run_case("fork")
    run_case("forkserver")
    run_raw_fork()
    time.sleep(1.5)  # let forkserver-spawned helper settle
