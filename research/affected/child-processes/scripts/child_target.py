import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import fp_module  # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"

fp_module.do_work(mode)

if mode == "exit0":
    sys.exit(0)
elif mode == "os_exit":
    os._exit(0)
elif mode == "sleep":
    import time

    time.sleep(30)
elif mode == "raise":
    raise RuntimeError("boom")
# else: fall through, normal interpreter shutdown -> atexit runs
