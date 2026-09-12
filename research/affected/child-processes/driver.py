import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid

VENV_PY = "/tmp/child-trace/venv/bin/python"
VENV313_PY = "/tmp/child-trace/venv313/bin/python"
NOOP_PY = "/tmp/child-trace/venv_noop/bin/python"
SCRIPT = "/tmp/child-trace/scripts/child_target.py"
ROOT = "/tmp/child-trace"
AFFECTED_BASE = "/tmp/child-trace/affected_runs"


def fresh_dir(tag):
    d = os.path.join(AFFECTED_BASE, f"{tag}-{uuid.uuid4().hex[:8]}")
    os.makedirs(d, exist_ok=True)
    return d


def base_env(affected_dir, test_id="t1", extra=None):
    env = dict(os.environ)
    env["VOCI_AFFECTED_DIR"] = affected_dir
    env["VOCI_ROOT"] = ROOT
    env["VOCI_TEST_ID"] = test_id
    if extra:
        env.update(extra)
    return env


def snapshot(d):
    started = sorted(f for f in os.listdir(d) if f.endswith(".started"))
    finished = sorted(f for f in os.listdir(d) if f.endswith(".finished"))
    jsons = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    reports = []
    for j in jsons:
        with open(os.path.join(d, j)) as f:
            reports.append(json.load(f))
    return {"started": started, "finished": finished, "reports": reports}


def report(name, d, extra_note=""):
    snap = snapshot(d)
    n_seen = sum(len(r["seen"]) for r in snap["reports"])
    print(f"[{name}] started={len(snap['started'])} finished={len(snap['finished'])} "
          f"json_reports={len(snap['reports'])} traced_calls={n_seen} {extra_note}")
    for r in snap["reports"]:
        print(f"    pid={r['pid']} test_id={r['test_id']} seen={r['seen']}")
    return snap


# ---------------------------------------------------------------- cases ----

def case_subprocess_run():
    d = fresh_dir("subprocess-run")
    subprocess.run([VENV_PY, SCRIPT, "normal"], env=base_env(d), check=True)
    report("subprocess.run([python, script])", d)


def case_sh_c_python():
    d = fresh_dir("sh-c-explicit")
    subprocess.run(["sh", "-c", f"{VENV_PY} {SCRIPT} normal"], env=base_env(d), check=True)
    report("sh -c '<venv-python> script.py' (explicit interpreter)", d)

    d2 = fresh_dir("sh-c-bare-python3")
    subprocess.run(["sh", "-c", f"python3 {SCRIPT} normal"], env=base_env(d2), check=True)
    report("sh -c 'python3 script.py' (bare, resolves to system python)", d2,
           "-- expect NOT reported: no .pth in system python")


def case_console_script():
    d = fresh_dir("console-script")
    subprocess.run(["/tmp/child-trace/venv/bin/tinytool"], env=base_env(d), check=True)
    report("console-script entry point (tinytool)", d)


def case_os_system():
    d = fresh_dir("os-system")
    env = base_env(d)
    old = dict(os.environ)
    os.environ.update(env)
    try:
        os.system(f"{VENV_PY} {SCRIPT} normal")
    finally:
        os.environ.clear()
        os.environ.update(old)
    report("os.system(...)", d)


def _mp_target():
    sys.path.insert(0, "/tmp/child-trace/scripts")
    import fp_module
    fp_module.do_work("mp-target")


def case_multiprocessing(method):
    d = fresh_dir(f"mp-{method}")
    env = base_env(d)
    old = dict(os.environ)
    os.environ.update(env)
    try:
        ctx = multiprocessing.get_context(method)
        p = ctx.Process(target=_mp_target)
        p.start()
        p.join(timeout=10)
    finally:
        os.environ.clear()
        os.environ.update(old)
    report(f"multiprocessing method={method}", d, f"exitcode={p.exitcode}")


def case_process_pool_executor():
    from concurrent.futures import ProcessPoolExecutor

    d = fresh_dir("ppe")
    env = base_env(d)
    old = dict(os.environ)
    os.environ.update(env)
    try:
        with ProcessPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_mp_target)
            fut.result(timeout=10)
    finally:
        os.environ.clear()
        os.environ.update(old)
    report("ProcessPoolExecutor (default mp context)", d)


def case_os_fork():
    d = fresh_dir("os-fork")
    env = base_env(d, test_id="fork-parent")
    old = dict(os.environ)
    os.environ.update(env)
    try:
        sys.path.insert(0, "/tmp/child-trace/scripts")
        import fp_module

        pid = os.fork()
        if pid == 0:
            fp_module.do_work("forked-child")
            os._exit(0)
        else:
            os.waitpid(pid, 0)
    finally:
        os.environ.clear()
        os.environ.update(old)
    report("raw os.fork() [note: driver runs under system python, no tracer installed]", d)


def case_kill_variants():
    for sig_name, sig in [("SIGTERM", signal.SIGTERM), ("SIGKILL", signal.SIGKILL)]:
        d = fresh_dir(f"kill-{sig_name}")
        p = subprocess.Popen([VENV_PY, SCRIPT, "sleep"], env=base_env(d))
        time.sleep(0.5)  # let it get past PY_START on do_work
        p.send_signal(sig)
        p.wait(timeout=5)
        report(f"child killed with {sig_name} mid-run", d, f"returncode={p.returncode}")


def case_timeout():
    d = fresh_dir("timeout")
    p = subprocess.Popen([VENV_PY, SCRIPT, "sleep"], env=base_env(d))
    time.sleep(0.5)
    try:
        p.wait(timeout=1)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    report("child that times out then parent .kill()s it", d)


def case_os_exit():
    d = fresh_dir("os-exit")
    subprocess.run([VENV_PY, SCRIPT, "os_exit"], env=base_env(d))
    report("child calls os._exit(0) itself (atexit never runs)", d)


def case_raise():
    d = fresh_dir("raise")
    subprocess.run([VENV_PY, SCRIPT, "raise"], env=base_env(d))
    report("child raises an uncaught exception (unclean exit, non-zero)", d)


def case_different_interpreter():
    d = fresh_dir("diff-interp-py313-with-pth")
    subprocess.run([VENV313_PY, SCRIPT, "normal"], env=base_env(d), check=True)
    report("different venv, 3.13, but WITH our .pth installed", d)

    d2 = fresh_dir("diff-interp-noop")
    subprocess.run([NOOP_PY, SCRIPT, "normal"], env=base_env(d2), check=True)
    report("different venv, 3.14, WITHOUT our .pth (simulates system python / uv run --python)", d2,
           "-- expect NOT reported")

    d3 = fresh_dir("system-python")
    sys_py = shutil.which("python3")
    if sys_py:
        subprocess.run([sys_py, SCRIPT, "normal"], env=base_env(d3))
        report(f"system python3 ({sys_py})", d3, "-- expect NOT reported")


def case_env_scrubbed():
    d = fresh_dir("env-scrubbed")
    # Only PATH survives -- VOCI_AFFECTED_DIR and VOCI_ROOT are dropped.
    minimal_env = {"PATH": os.environ.get("PATH", "")}
    subprocess.run([VENV_PY, SCRIPT, "normal"], env=minimal_env)
    report("child spawned with env={'PATH': ...} (drops VOCI_AFFECTED_DIR)", d,
           "-- expect NOT reported (dir won't even exist to check, this call just confirms no crash)")


if __name__ == "__main__":
    os.makedirs(AFFECTED_BASE, exist_ok=True)
    case_subprocess_run()
    case_sh_c_python()
    case_console_script()
    case_os_system()
    for method in ("spawn", "fork", "forkserver"):
        case_multiprocessing(method)
    case_process_pool_executor()
    case_os_fork()
    case_kill_variants()
    case_timeout()
    case_os_exit()
    case_raise()
    case_different_interpreter()
    case_env_scrubbed()
