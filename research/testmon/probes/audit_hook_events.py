import sys, pathlib, os, threading, contextvars, json, sqlite3, tempfile
seen = []
def hook(ev, args):
    if ev in ("open", "os.listdir", "os.scandir", "subprocess.Popen", "os.posix_spawn", "os.exec", "sqlite3.connect", "import", "glob.glob"):
        seen.append((ev, str(args[0])[:40]))
sys.addaudithook(hook)
p = pathlib.Path(tempfile.mkdtemp()) / "d.json"
p.write_text("{}")
seen.clear()
json.loads(p.read_text()); open(p).close(); os.listdir(p.parent); list(p.parent.glob("*"))
import subprocess; subprocess.run(["true"])
sqlite3.connect(":memory:")
import csv  # import event
print(seen)
cv = contextvars.ContextVar("cv", default=None); cv.set("T")
out = []
t = threading.Thread(target=lambda: out.append(cv.get())); t.start(); t.join()
print("thread sees", out, "flag", getattr(sys.flags, "thread_inherit_context", None))
