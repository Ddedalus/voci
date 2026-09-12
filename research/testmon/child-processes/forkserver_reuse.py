import multiprocessing, os, sys, time
sys.path.insert(0, "/tmp/child-trace/scripts")
import fp_module

def target(tag):
    fp_module.do_work(tag)

if __name__ == "__main__":
    ctx = multiprocessing.get_context("forkserver")
    for i, tid in enumerate(["first-test", "second-test", "third-test"]):
        os.environ["VOCI_TEST_ID"] = tid
        p = ctx.Process(target=target, args=(f"call-{i}",))
        p.start()
        p.join()
    time.sleep(1)
