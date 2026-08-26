"""`python -m velox._run._isolated_worker CONFIG_PATH RESULT_PATH` -- one `@velox.isolated`
test's whole subprocess.

Not a public entry point; spawned only by `isolated.run_isolated`. Reads its instructions from
`CONFIG_PATH` (written by the parent), re-collects the one file naming the target test on this
fresh interpreter, runs it through this package's own `run_suite`, and writes the resulting
`TestResult` to `RESULT_PATH` as JSON (`isolated.result_to_json`'s shape). A target that can't be
found on re-collection -- the file changed underfoot, most likely -- is reported as its own
`error` result rather than left for the parent to time out waiting on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from velox._assertions import rewrite as _rewrite
from velox._collection import collect as _collect
from velox._run import run as _run
from velox._run.isolated import result_to_json


def main(argv: list[str] | None = None) -> int:
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
                    f"@velox.isolated: could not re-collect {target_id!r} in its subprocess: "
                    f"{reason}"
                ),
                "failure_summary": "isolated subprocess re-collection failed",
                "captured_stdout": "",
                "captured_stderr": "",
                "log_records": [],
            }
        else:
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
            )
            data = result_to_json(results[0])
    finally:
        if not hook_already_installed:
            _rewrite.uninstall()

    Path(result_path).write_text(json.dumps(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
