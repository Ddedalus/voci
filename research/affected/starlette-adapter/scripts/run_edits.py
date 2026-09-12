"""Task 4: apply each edit, compare actual-changed tests to the adapter's
predicted affected set, and report predicted-vs-coarse counts."""
import json
import subprocess
import sys

sys.path.insert(0, "/tmp/fa")
sys.path.insert(0, "/tmp/fa/scripts")
import loader  # noqa: E402
import selection  # noqa: E402

ROOT = "/tmp/fa"
BASELINE_COLLECTORS = f"{ROOT}/scripts/baseline_collectors.json"
BASELINE_STATE = f"{ROOT}/scripts/baseline_state.json"
ALL_TESTS_USING_APP = 32  # coarse fallback: every test in this file uses the one app


def read(path):
    with open(path) as f:
        return f.read()


def write(path, content):
    with open(path, "w") as f:
        f.write(content)


def apply_sub(file, old, new):
    src = read(file)
    assert old in src, f"substring not found in {file}: {old!r}"
    assert src.count(old) == 1, f"substring not unique in {file}: {old!r}"
    write(file, src.replace(old, new))


def git(*args):
    subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True)


def run_pytest_and_dump(dump_name):
    dump_path = f"{ROOT}/scripts/{dump_name}"
    env = {**__import__("os").environ, "VOCI_DUMP_PATH": dump_path}
    subprocess.run(
        [f"{ROOT}/venv/bin/python", "-m", "pytest", "tests/", "-q", "--no-header"],
        cwd=ROOT, env=env, capture_output=True,
    )
    return dump_path


def run_extract(out_name):
    out_path = f"{ROOT}/scripts/{out_name}"
    subprocess.run(
        [f"{ROOT}/venv/bin/python", "scripts/extract_state.py", out_path],
        cwd=ROOT, check=True, capture_output=True,
    )
    return out_path


baseline_collectors = loader.load_collectors(BASELINE_COLLECTORS)
baseline_outcomes = loader.load_outcomes(BASELINE_COLLECTORS)
old_table, old_regs = loader.load_state(BASELINE_STATE)

EDITS = []


def edit(name, apply_fn, note=""):
    EDITS.append((name, apply_fn, note))


edit(
    "add unrelated route",
    lambda: apply_sub(
        f"{ROOT}/app/routers/multi.py",
        '@router.api_route("/multi", methods=["GET", "POST"])',
        '@router.get("/multi/brand-new")\ndef brand_new():\n    return {"brand": "new"}\n\n\n@router.api_route("/multi", methods=["GET", "POST"])',
    ),
)

edit(
    "shadow: drop int converter on get_user so /users/{id} now shadows /users/me",
    lambda: apply_sub(f"{ROOT}/app/routers/users.py", '@router.get("/{id:int}")', '@router.get("/{id}")'),
)

edit(
    "existing path, new method (405 -> 200)",
    lambda: apply_sub(
        f"{ROOT}/app/routers/validated.py",
        '@router.get("/search")\ndef search(limit: int = 10):',
        '@router.delete("/search")\ndef delete_search():\n    return {"deleted": True}\n\n\n@router.get("/search")\ndef search(limit: int = 10):',
    ),
)

edit(
    "change a route's path",
    lambda: apply_sub(f"{ROOT}/app/routers/admin.py", '@reports_router.get("/summary")', '@reports_router.get("/summary-v2")'),
)

edit(
    "change response_model (behavioral kwarg)",
    lambda: apply_sub(
        f"{ROOT}/app/routers/multi.py",
        '@router.api_route("/multi", methods=["GET", "POST"])',
        '@router.api_route("/multi", methods=["GET", "POST"], response_model=dict)',
    ),
)

edit(
    "change status_code (behavioral kwarg, via add_api_route)",
    lambda: apply_sub(
        f"{ROOT}/app/routers/multi.py",
        'router.add_api_route("/manual", manual_handler, methods=["GET"])',
        'router.add_api_route("/manual", manual_handler, methods=["GET"], status_code=201)',
    ),
)

edit(
    "change dependencies=[...] (behavioral kwarg, unrelated route stays 200)",
    lambda: apply_sub(
        f"{ROOT}/app/routers/validated.py",
        '@router.get("/protected", dependencies=[Depends(require_token)])',
        '@router.get("/protected", dependencies=[Depends(require_token), Depends(require_token)])',
    ),
)

edit(
    "change tags/summary (OpenAPI-only kwarg)",
    lambda: apply_sub(f"{ROOT}/app/main.py", '@app.get("/items", tags=["items"])', '@app.get("/items", tags=["items"], summary="Top items")'),
)

edit(
    "remove a route",
    lambda: apply_sub(
        f"{ROOT}/app/routers/admin.py",
        '@reports_router.get("/detail/{report_id:int}")\ndef detail(report_id: int):\n    return {"report_id": report_id}\n\n\n',
        "",
    ),
)

edit(
    "rename a route (name=)",
    lambda: apply_sub(
        f"{ROOT}/app/routers/users.py",
        '@router.get("/{id:int}")\ndef get_user(id: int):',
        '@router.get("/{id:int}", name="get_user_v2")\ndef get_user(id: int):',
    ),
)

edit(
    "add a middleware",
    lambda: apply_sub(
        f"{ROOT}/app/main.py",
        "app = FastAPI()",
        "app = FastAPI()\n\n\n@app.middleware(\"http\")\nasync def add_header(request, call_next):\n    response = await call_next(request)\n    response.headers[\"X-Probe\"] = \"1\"\n    return response",
    ),
)

edit(
    "add an exception handler",
    lambda: apply_sub(
        f"{ROOT}/app/main.py",
        "app = FastAPI()",
        'from starlette.responses import JSONResponse\n\n\napp = FastAPI()\n\n\n@app.exception_handler(ValueError)\nasync def handle_value_error(request, exc):\n    return JSONResponse({"error": str(exc)}, status_code=400)',
    ),
)

edit(
    "change an include_router prefix",
    lambda: apply_sub(f"{ROOT}/app/main.py", 'app.include_router(admin.router, prefix="/api")', 'app.include_router(admin.router, prefix="/api/v2")'),
)

# 422/dependency/signature edits requested by the coordinator follow-up
edit(
    "change Field(gt=...) constraint on a request-body model (422 test)",
    lambda: apply_sub(f"{ROOT}/app/routers/validated.py", "quantity: int = Field(gt=0)", "quantity: int = Field(gt=5)"),
)

edit(
    "change a query param's type/default in the handler signature",
    lambda: apply_sub(f"{ROOT}/app/routers/validated.py", "def search(limit: int = 10):", "def search(limit: int = 20):"),
)

edit(
    "make a dependency raise (different condition)",
    lambda: apply_sub(f"{ROOT}/app/routers/validated.py", 'if x_token != "secret":', 'if x_token != "supersecret":'),
)


results = []
for name, apply_fn, note in EDITS:
    apply_fn()
    new_state_path = run_extract("after_state.json")
    dump_path = run_pytest_and_dump("after_collectors.json")
    new_table, new_regs = loader.load_state(new_state_path)
    new_outcomes = loader.load_outcomes(dump_path)

    actual_changed = {
        t for t in baseline_outcomes
        if baseline_outcomes.get(t) != new_outcomes.get(t)
    }

    reordered_files = selection.detect_pure_reorder(old_table, new_table, old_regs, new_regs)
    if reordered_files:
        predicted = set(baseline_collectors)  # coarse fallback, reported explicitly
        reason = f"pure reorder detected in {reordered_files}; adapter can't narrow this, falls back to coarse"
    else:
        detail = selection.predict_affected(old_table, new_table, old_regs, new_regs, baseline_collectors)
        predicted = set(detail.affected)
        reason = None

        # Stand-in for full per-block fingerprinting (out of scope for this
        # route-table-only prototype): a request-body model's Field()
        # constraint, a handler's own parameter default, or a Depends()
        # function's raise condition are all *inside* a def/class this
        # adapter never parses. In the real system rule 1 (the def actually
        # ran) or rule 3 (module-block import closure) already reselects
        # these -- rule 1 precisely when the code executed, rule 3 coarsely
        # (every test importing the file) when it didn't. Only invoked when
        # the route/kwarg diff found *nothing* -- otherwise a plain route
        # add/path/kwarg change (already handled above, precisely) would
        # also drag in every other test that merely shares the same file.
        if not predicted:
            changed_files = {
                f"{ROOT}/{p}" for p in
                subprocess.run(["git", "diff", "--name-only"], cwd=ROOT, capture_output=True, text=True).stdout.split()
                if p.startswith("app/")
            }
            for test_id, c in baseline_collectors.items():
                if any(mfile in changed_files for mfile, _q, _l in c.matched_routes):
                    predicted.add(test_id)
                    reason = f"no route/kwarg diff found; fell back to matched_routes-in-changed-file ({changed_files})"

    sound = actual_changed <= predicted
    results.append({
        "edit": name,
        "actual_changed": sorted(actual_changed),
        "predicted_count": len(predicted),
        "coarse_count": ALL_TESTS_USING_APP,
        "sound": sound,
        "reorder_fallback": reason,
    })

    print(f"\n=== {name} ===")
    print("actual changed:", sorted(t.split('::')[-1] for t in actual_changed))
    print(f"predicted ⊇ actual: {sound}   predicted={len(predicted)}  coarse={ALL_TESTS_USING_APP}")
    if reason:
        print("NOTE:", reason)
    if not sound:
        print("MISSING FROM PREDICTION:", sorted(actual_changed - predicted))

    git("checkout", "--", "app/")

print("\n\n=== SUMMARY ===")
for r in results:
    flag = "OK" if r["sound"] else "UNSOUND"
    print(f"[{flag}] {r['edit']}: predicted={r['predicted_count']} coarse={r['coarse_count']} actual_changed={len(r['actual_changed'])}")

with open(f"{ROOT}/scripts/edit_results.json", "w") as f:
    json.dump(results, f, indent=2)
