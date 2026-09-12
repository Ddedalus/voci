import contextvars, threading
from fastapi import FastAPI
from fastapi.testclient import TestClient
cv = contextvars.ContextVar("cv", default=None)
app = FastAPI()
seen = {}
@app.get("/a")
async def a():
    seen.setdefault("async", []).append((cv.get(), threading.current_thread().name)); return {}
@app.get("/s")
def s():
    seen.setdefault("sync", []).append((cv.get(), threading.current_thread().name)); return {}

cv.set("plain-client")
c = TestClient(app); c.get("/a"); c.get("/s")
cv.set("fixture")
with TestClient(app) as c2:          # persistent portal, as in a session fixture
    cv.set("test-1"); c2.get("/a"); c2.get("/s")
    def other():
        cv.set("test-2-thread"); c2.get("/a"); c2.get("/s")
    t = threading.Thread(target=other); t.start(); t.join()
for k, v in seen.items(): print(k, v)
import anyio, starlette; print("anyio", anyio.__version__, "starlette", starlette.__version__)
