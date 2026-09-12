"""Does the sys.monitoring registration survive os.fork(), and does our
register_at_fork hook let us manually flush before an os._exit that would
otherwise skip atexit?
"""
import os
import sys

sys.path.insert(0, "/tmp/child-trace/scripts")
import fp_module  # noqa: E402
import _voci_child_tracer as tracer  # noqa: E402


def main():
    print("parent pid", os.getpid())
    print("parent _TOOL_ID:", tracer._TOOL_ID)
    fp_module.do_work("parent-before-fork")
    print("parent _seen after parent work:", tracer._seen)

    pid = os.fork()
    if pid == 0:
        # Child: check whether the same tool id / callback is still active
        # purely from the fork's memory copy, with NO re-registration call.
        print("child pid", os.getpid(), "_TOOL_ID still:", tracer._TOOL_ID)
        try:
            still_registered = sys.monitoring.get_tool(tracer._TOOL_ID)
        except ValueError:
            still_registered = "<tool id invalid post-fork>"
        print("child sys.monitoring.get_tool(tool_id):", still_registered)
        fp_module.do_work("forked-child-manual-flush-test")
        print("child _seen after child-side work (same set object, inherited):", tracer._seen)
        # Manually flush before the os._exit that a real mp fork-child would do,
        # to prove the data *could* be saved if something called flush().
        tracer._flush()
        os._exit(0)
    else:
        os.waitpid(pid, 0)
        print("parent done waiting")


if __name__ == "__main__":
    main()
