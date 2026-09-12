# Research behind the affected-test selection plan

Throwaway prototypes, probes and subagent reports from the 2026-09-12 research pass behind
[plans/testmon-plan.md](../../plans/testmon-plan.md). Reference material only: excluded from
ruff and pyrefly (`pyproject.toml`), never collected by voci, never imported by it. The code was
written to answer one question each, fast; don't copy it into `voci/` without rewriting it.

Everything ran on CPython 3.14 (some probes also on 3.13), under WSL2. The venvs weren't kept.
Recreate one with `uv venv <dir>/venv --python 3.14` plus the packages named below. Hardcoded
`/tmp/...` paths inside the scripts point at where each one originally lived.

## Reports

Each report is a subagent's final write-up, kept verbatim with a short header.

| Report | Backs plan section |
| --- | --- |
| [testmon-docs-and-issues.md](reports/testmon-docs-and-issues.md) | Failure modes, Storage, Decisions |
| [testmon-source-mechanisms.md](reports/testmon-source-mechanisms.md) | Fingerprints, Environment key, Storage |
| [voci-integration-seams.md](reports/voci-integration-seams.md) | Work to do (M1–M3), Tracer |
| [fingerprint-probes.md](reports/fingerprint-probes.md) | Fingerprints, Tracer (its Q8 is corrected in its header) |
| [child-processes.md](reports/child-processes.md) | Child processes |
| [name-deps.md](reports/name-deps.md) | What a passing test depends on |
| [starlette-adapter.md](reports/starlette-adapter.md) | Starlette/FastAPI adapter |

## Code

- [`probes/`](probes/): single-file checks run directly while planning.
  - `sysmon_callback_cost.py`: `sys.monitoring` callback variants against a baseline, the
    source of the Tracer cost figures.
  - `audit_hook_events.py`: which audit events fire for `open`, listings, spawns, sqlite, and
    whether new threads inherit context.
  - `environ_recording.py`: the recording `os.environ` subclass.
  - `testclient_context.py`: anyio carries the caller's ContextVar into `TestClient` handlers,
    including through a persistent portal. Needs `fastapi`, `httpx`.
  - `watch_reload.sh`: today's `--watch` running stale first-party code.
- [`fingerprint-probes/`](fingerprint-probes/): `q1`–`q8` scripts plus their sample modules.
  They cover qualname correspondence, duplicate qualnames, which fingerprint catches which edit,
  `ast.dump` vs text hashing cost, module-level attribution, assertion rewriting,
  generated/zipimport/pyc-only code, and callback cost. `attrs_mod.py` needs `attrs`.
  `q7`'s pyc-only case needs `pyc_only/pmod.pyc`: compile a small `_src_pmod.py` and delete
  the source. The `.pyc` itself is gitignored, so it wasn't kept.
- [`child-processes/`](child-processes/): the `.pth`-loaded child tracer
  (`_voci_child_tracer.py`), the which-children-report matrix (`driver.py`), and the fork,
  forkserver-reuse, env-race and `multiprocessing` piggyback experiments. `tinypkg/` is the
  console-script fixture. Needs a venv with the tracer installed as a `.pth`, see its report §2.
- [`name-deps/`](name-deps/): the per-statement analyzer (`namedeps.py`), the synthetic project
  and soundness harness (`proj/`, `soundness.py`), and the precision measurements over
  `oss/fastapi` and `oss/httpx` (`precision.py`, `precision_httpx.py`). The analyzer only
  parses, so it needs nothing installed. `proj/` imports `pydantic` and `sqlalchemy`.
- [`starlette-adapter/`](starlette-adapter/): the recorder and route-table extraction
  (`adapter.py`), the static route parser (`staticparse.py`), the predictor (`selection.py`),
  the synthetic app (`app/`), and the 32-test suite (`tests/`). `scripts/run_edits.py` is the
  16-edit soundness harness, and `scripts/edit_results.json` its last output. Needs `fastapi`,
  `httpx`, `pytest`.
