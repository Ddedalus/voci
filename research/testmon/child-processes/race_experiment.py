"""Two "tests" running concurrently in threads, each spawning children and
claiming a test id. Compares:
  (a) naive: mutate the shared os.environ then Popen(..., env=None)
  (b) safe:  build a private env dict per call, pass env=<dict> explicitly

Each child reports which VOCI_TEST_ID it actually saw. A mismatch between
the spawning thread's id and the id the child reports is a misattribution.
"""
import os
import subprocess
import sys
import threading
import time
import uuid

VENV_PY = "/tmp/child-trace/venv/bin/python"
SCRIPT = "/tmp/child-trace/scripts/child_target.py"
ROOT = "/tmp/child-trace"


def run_naive(thread_id, n, affected_dir, mismatches, lock):
    for i in range(n):
        os.environ["VOCI_TEST_ID"] = thread_id  # global mutation, racy
        os.environ["VOCI_AFFECTED_DIR"] = affected_dir
        os.environ["VOCI_ROOT"] = ROOT
        subprocess.run([VENV_PY, SCRIPT, "normal"], env=None, check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_safe(thread_id, n, affected_dir, mismatches, lock):
    for i in range(n):
        env = dict(os.environ)  # snapshot, private to this call
        env["VOCI_TEST_ID"] = thread_id
        env["VOCI_AFFECTED_DIR"] = affected_dir
        env["VOCI_ROOT"] = ROOT
        subprocess.run([VENV_PY, SCRIPT, "normal"], env=env, check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def experiment(name, worker, n_per_thread=150):
    import glob
    import json
    import shutil

    d = f"/tmp/child-trace/race_{name}_{uuid.uuid4().hex[:6]}"
    os.makedirs(d, exist_ok=True)
    mismatches = []
    lock = threading.Lock()
    threads = [
        threading.Thread(target=worker, args=("thread-A", n_per_thread, d, mismatches, lock)),
        threading.Thread(target=worker, args=("thread-B", n_per_thread, d, mismatches, lock)),
    ]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.time() - t0

    # We can't recover "who spawned which pid" after the fact from env alone
    # in the naive case (that's the point) -- but every report should at
    # least be self-consistent: since child_target's own imports also touch
    # VOCI_TEST_ID only via the tracer, the only signal we have post-hoc is
    # whether the *set* of ids used matches expectation and whether id
    # assignment looks "clumpy" (long same-id runs => lost interleaving,
    # not proof of misattribution by itself). So instead we correlate by
    # wall-clock: each thread also records, locally, the id it *intended*
    # to use immediately before each Popen call, tagged with the child's
    # pid (returned by Popen). We compare that to what the child reported.
    print(f"[{name}] {2 * n_per_thread} children in {dt:.2f}s")
    reports = {}
    for f in glob.glob(os.path.join(d, "*.json")):
        r = json.load(open(f))
        reports[r["pid"]] = r["test_id"]
    return d, reports


class TrackingPopen:
    """Wraps subprocess.Popen to record (pid -> intended test id) so we can
    check what the child actually saw against what the spawning thread meant.
    """
    def __init__(self):
        self.intended = {}
        self.lock = threading.Lock()

    def run_naive(self, thread_id, n, affected_dir):
        for _ in range(n):
            os.environ["VOCI_TEST_ID"] = thread_id
            os.environ["VOCI_AFFECTED_DIR"] = affected_dir
            os.environ["VOCI_ROOT"] = ROOT
            p = subprocess.Popen([VENV_PY, SCRIPT, "normal"], env=None,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with self.lock:
                self.intended[p.pid] = thread_id
            p.wait()

    def run_safe(self, thread_id, n, affected_dir):
        for _ in range(n):
            env = dict(os.environ)
            env["VOCI_TEST_ID"] = thread_id
            env["VOCI_AFFECTED_DIR"] = affected_dir
            env["VOCI_ROOT"] = ROOT
            p = subprocess.Popen([VENV_PY, SCRIPT, "normal"], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with self.lock:
                self.intended[p.pid] = thread_id
            p.wait()


def experiment2(name, method_name, n_per_thread=200):
    import glob
    import json

    d = f"/tmp/child-trace/race2_{name}_{uuid.uuid4().hex[:6]}"
    os.makedirs(d, exist_ok=True)
    tp = TrackingPopen()
    method = getattr(tp, method_name)
    threads = [
        threading.Thread(target=method, args=("thread-A", n_per_thread, d)),
        threading.Thread(target=method, args=("thread-B", n_per_thread, d)),
    ]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.time() - t0

    mismatches = 0
    total = 0
    for f in glob.glob(os.path.join(d, "*.json")):
        r = json.load(open(f))
        total += 1
        intended = tp.intended.get(r["pid"])
        if intended is not None and intended != r["test_id"]:
            mismatches += 1
    print(f"[{name}] {total} children reported, {mismatches} misattributed "
          f"({100 * mismatches / total:.1f}%), {dt:.2f}s for {2 * n_per_thread} spawns")
    return mismatches, total


if __name__ == "__main__":
    print("=== naive: mutate shared os.environ, Popen(env=None) ===")
    experiment2("naive", "run_naive")
    print("=== safe: build a private env dict per call, Popen(env=<dict>) ===")
    experiment2("safe", "run_safe")
