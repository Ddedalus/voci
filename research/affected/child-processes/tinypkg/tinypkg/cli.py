import os
import sys

sys.path.insert(0, "/tmp/child-trace/scripts")
import fp_module


def main():
    fp_module.do_work("console-script")
    print("tinytool ran, pid", os.getpid())
