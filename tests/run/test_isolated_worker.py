"""`voci._run._isolated_worker.main`: one `@voci.isolated` test's whole subprocess, exercised
in-process rather than as a real spawn -- `tests/test_cli.py` covers the real spawn end to end;
this covers the worker's own `Tracer`/`on_test_dependencies` wiring, which needs a real module-
scope fixture on disk to exercise at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from voci._affected import collector as _collector
from voci._run import _isolated_worker


def _write_config(
    *,
    rootdir: Path,
    file: Path,
    target_id: str,
    basetemp: Path,
) -> Path:
    config_path = rootdir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "rootdir": str(rootdir),
                "file": str(file),
                "target_id": target_id,
                "index": 0,
                "timeout": None,
                "loop_watchdog": None,
                "teardown_grace": None,
                "filterwarnings": [],
                "basetemp": str(basetemp),
                "assert_mode": "plain",
                "assert_cache_dir": None,
                "rewrite_roots": [],
            }
        )
    )
    return config_path


def test_main_folds_a_module_scope_fixtures_collector_into_the_shipped_record(
    tmp_path: Path,
) -> None:
    """The worker's own `run_suite` call now passes `on_test_dependencies`, not bare
    `on_collector` -- the shipped `collector` key must include what a module-scope fixture's own
    collector saw, not just the test's own envelope."""
    test_file = tmp_path / "test_mod.py"
    test_file.write_text(
        "import voci\n\n"
        "@voci.fixture(scope='module')\n"
        "def per_module():\n"
        "    return 1\n\n"
        "async def test_isolated(x: int = voci.Depends(per_module)) -> None:\n"
        "    pass\n"
    )
    basetemp = tmp_path / "basetemp"
    config_path = _write_config(
        rootdir=tmp_path,
        file=test_file,
        target_id="test_mod.py::test_isolated",
        basetemp=basetemp,
    )
    result_path = tmp_path / "result.json"

    # The worker starts its own Tracer internally; no ambient one is needed here -- unlike
    # test_run.py's own Tracer-based tests, which exercise run_suite directly and so must supply
    # one themselves.
    assert _collector.current_collector.get() is None
    status = _isolated_worker.main([str(config_path), str(result_path)])
    assert status == 0

    data = json.loads(result_path.read_text())
    assert data["outcome"] == "passed"
    collector_data = data["collector"]
    assert collector_data is not None
    qualnames = {qualname for _filename, qualname in collector_data["codes"]}
    assert "test_isolated" in qualnames
    assert "per_module" in qualnames
