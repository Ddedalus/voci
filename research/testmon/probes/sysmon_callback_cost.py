import sys, time, statistics
mon = sys.monitoring
TOOL = 3
def f(x): return x + 1
def work(n=2_000_000):
    s = 0
    for i in range(n): s = f(s)
    return s
def timeit(label, setup=None, teardown=None):
    ts = []
    for _ in range(5):
        if setup: setup()
        t = time.perf_counter(); work(); ts.append(time.perf_counter() - t)
        if teardown: teardown()
    print(f"{label:40s} {statistics.median(ts)*1000:7.1f} ms")
    return statistics.median(ts)
timeit("warmup"); base = timeit("baseline")
seen = set()
def cb_tuple(code, off): seen.add((code.co_filename, code.co_qualname))
def cb_obj(code, off): seen.add(code)
def cb_dedup(code, off):
    if code in seen: return
    seen.add(code)
def cb_id(code, off): seen.add(id(code))
def cb_id_dedup(code, off):
    i = id(code)
    if i in seen: return
    seen.add(i)
def cb_noop(code, off): return None
def cb_disable(code, off): return mon.DISABLE
for name, cb in [("tuple every call", cb_tuple), ("code obj set.add", cb_obj), ("dedup check", cb_dedup), ("id set.add", cb_id), ("id dedup", cb_id_dedup), ("noop callback", cb_noop), ("DISABLE", cb_disable)]:
    def setup(cb=cb):
        seen.clear(); mon.use_tool_id(TOOL, "p"); mon.register_callback(TOOL, mon.events.PY_START, cb); mon.set_events(TOOL, mon.events.PY_START)
    def teardown():
        mon.set_events(TOOL, 0); mon.register_callback(TOOL, mon.events.PY_START, None); mon.free_tool_id(TOOL); mon.restart_events()
    t = timeit(name, setup, teardown); print(f"{'':40s} {t/base:.2f}x")
