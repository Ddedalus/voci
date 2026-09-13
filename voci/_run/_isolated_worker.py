"""`python -m voci._run._isolated_worker CONFIG_PATH RESULT_PATH` -- one `@voci.isolated`
test's whole subprocess.

Not a public entry point; spawned only by `isolated.run_isolated`. Reads its instructions from
`CONFIG_PATH` (written by the parent), re-collects the one file naming the target test on this
fresh interpreter, runs it through this package's own `run_suite`, and writes the resulting
`TestResult` to `RESULT_PATH` as JSON (`isolated.result_to_json`'s shape). A target that can't be
found on re-collection -- the file changed underfoot, most likely -- is reported as its own
`error` result rather than left for the parent to time out waiting on.

Under a `coverage run` the parent asks for this process to be measured too, through the
environment; `coverage.py` next door explains how the two halves meet. Affected-test recording
rides the same idea: this process starts its own `Tracer`, since a fresh interpreter shares no
tool id or ContextVar with the parent's, and `result_to_json`'s `collector` key carries what it
saw back across the same boundary (`isolated.py`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from voci._affected import collector as _collector
from voci._affected.tracer import Tracer
from voci._assertions import rewrite as _rewrite
from voci._collection import collect as _collect
from voci._run import coverage as _coverage
from voci._run import run as _run
from voci._run.isolated import result_to_json


def main(argv: list[str] | None = None) -> int:
    # Before the target module is imported below: a module already imported when measurement
    # starts records none of the lines its body ran. A no-op unless the parent run is itself
    # under coverage.py, and usually one even then -- see `coverage.start_in_subprocess`.
    _coverage.start_in_subprocess()

    args = sys.argv[1:] if argv is None else argv
    config_path, result_path = args

    config = json.loads(Path(config_path).read_text())
    rootdir = Path(config["rootdir"])
    file_path = Path(config["file"])
    target_id = config["target_id"]

    # Mirrors cli.main's own import setup: rootdir on sys.path once, before the target module is
    # imported, so a plain absolute import rooted at rootdir resolves the same way it does in the
    # parent process.
    rootdir_str = str(rootdir)
    if rootdir_str not in sys.path:
        sys.path.insert(0, rootdir_str)

    # Same idempotence guard as cli.main: only uninstall a hook this call is the one that
    # installed, in case something upstream (an embedding caller, conceivably) already had one.
    hook_already_installed = _rewrite.installed_hook() is not None
    _rewrite.install(
        [Path(p) for p in config["rewrite_roots"]],
        mode=config["assert_mode"],
        cache_dir=config["assert_cache_dir"],
        warn=False,
    )
    # This subprocess's own sys.monitoring tool: a fresh interpreter shares no tool id or
    # ContextVar with the parent, so the one test running here needs its own Tracer rather than
    # reusing anything the parent already started (plan's Tracer design, "Recording"). A
    # candidate id already taken -- vanishingly unlikely in a process dedicated to one test --
    # just leaves every collector below empty, same as a run nothing is tracing looks today.
    tracer = Tracer(rootdir, _collector.record_first_party)
    tracer.start()
    try:
        try:
            collected = _collect.collect([file_path], rootdir=rootdir)
            target = next((record for record in collected.records if record.id == target_id), None)
            if target is None:
                detail = "; ".join(error.message for error in collected.errors)
                reason = detail or "no matching test id came back from re-collection"
                data = {
                    "id": target_id,
                    "index": config["index"],
                    "outcome": "error",
                    "duration": 0.0,
                    "failure": (
                        f"@voci.isolated: could not re-collect {target_id!r} in its subprocess: "
                        f"{reason}"
                    ),
                    "failure_summary": "isolated subprocess re-collection failed",
                    "captured_stdout": "",
                    "captured_stderr": "",
                    "log_records": [],
                    "collector": None,
                }
            else:
                collected_records: list[_collector.CollectorRecord] = []
                results = _run.run_suite(
                    [target],
                    concurrency=1,
                    timeout=config["timeout"],
                    capture_passthrough=False,
                    basetemp=Path(config["basetemp"]),
                    # Both come from the parent's own run: a watchdog the user switched off
                    # stays off in here, and a cancelled test gets the same teardown budget on
                    # either side of the process boundary. `None` is `run_suite`'s own "use the
                    # default", which is what an older parent's config file leaves behind.
                    loop_watchdog=config.get("loop_watchdog"),
                    teardown_grace=config.get("teardown_grace") or _run.DEFAULT_TEARDOWN_GRACE,
                    filterwarnings=config.get("filterwarnings") or (),
                    already_isolated=True,
                    # Exactly one call for the one test this subprocess runs -- what
                    # result_to_json ships back below, the way coverage.py's own measurement
                    # crosses the same boundary (see this module's docstring).
                    on_collector=lambda _id, record: collected_records.append(record),
                )
                data = result_to_json(
                    results[0],
                    collector=collected_records[0] if collected_records else None,
                )
        finally:
            if not hook_already_installed:
                _rewrite.uninstall()
    finally:
        tracer.stop()

    Path(result_path).write_text(json.dumps(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
