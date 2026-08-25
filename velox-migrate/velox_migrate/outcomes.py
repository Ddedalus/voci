"""The pytest plugin that dumps what every test actually did, as JSON:

    pytest -p velox_migrate.outcomes --outcomes-out pytest-outcomes.json

`verify` needs one outcome per test id from each runner, and pytest's terminal output is a
summary rather than a record. This reads the reports pytest itself builds, so a suite's plugins,
xfail marks and setup errors are classified the way pytest classifies them and not the way a line
of output happens to read.

Like `extractor.py`, this module imports stdlib and pytest and nothing else — the pre-migration
tree is often the environment that can least afford another install, so the plugin has to survive
being copied next to the suite and loaded with `-p outcomes`.
"""

from __future__ import annotations

import json
import os

import pytest

# Bumped whenever the record's shape changes, and refused by `verify` when it does not match: a
# baseline recorded before a conversion is read back after it, potentially by a newer tool.
OUTCOMES_VERSION = 1

DEFAULT_OUT = os.path.join(".velox-migrate", "pytest-outcomes.json")

# What one test can end up as. Ordered by which wins when a test reports more than once: a call
# that failed outranks a teardown that errored, and any verdict outranks a plain pass, so the
# recorded outcome is the strongest thing that happened to that id.
PRECEDENCE = ("failed", "error", "xpassed", "xfailed", "skipped", "passed")


def pytest_addoption(parser):
    group = parser.getgroup("velox-migrate")
    group.addoption(
        "--outcomes-out",
        dest="outcomes_out",
        default=None,
        metavar="PATH",
        help=(
            f"Write the velox-migrate outcomes record here (default: {DEFAULT_OUT}; the "
            "OUTCOMES_OUT environment variable also works)."
        ),
    )


# Module-level for the same reason the extractor's are: a plugin is loaded once per run, and the
# report hooks filling these have no session to hang them off. Cleared per run so a second
# session in the same process does not inherit the first's.
_outcomes = {}
_collection_errors = []


def pytest_configure(config):
    _outcomes.clear()
    _collection_errors.clear()


def pytest_collectreport(report):
    # A module that fails to import contributes no test reports at all, and a record that did not
    # say so would look like a suite that is simply smaller than the one being verified against.
    if report.failed:
        _collection_errors.append(report.nodeid or ".")


def pytest_runtest_logreport(report):
    outcome = _outcome_of(report)
    if outcome is None:
        return
    _outcomes[report.nodeid] = _stronger(_outcomes.get(report.nodeid), outcome)


def _outcome_of(report):
    """What `report` says its test ended up as, or `None` when it says nothing — a setup or
    teardown that passed, which is only half of a verdict the call phase carries."""
    expected_failure = hasattr(report, "wasxfail")
    if report.when == "call":
        if expected_failure:
            # pytest reports an expected failure as a skip and an unexpected pass as a pass. A
            # *strict* unexpected pass arrives as a plain failure with no `wasxfail`, and stays
            # one here, which is what velox reports for it too.
            return "xpassed" if report.outcome == "passed" else "xfailed"
        return report.outcome
    if report.outcome == "failed":
        # Neither setup nor teardown is the test's own code, so a raise in either is an error
        # rather than a failure — the same line velox draws.
        return "error"
    if report.outcome == "skipped":
        # A skip before the call phase: a `skip`/`skipif` mark, or `pytest.skip()` in a fixture.
        # `wasxfail` here is an xfail mark on a test that never ran, which is still an xfail.
        return "xfailed" if expected_failure else "skipped"
    return None


def _stronger(previous, outcome):
    if previous is None:
        return outcome
    return min((previous, outcome), key=_rank)


def _rank(outcome):
    # An outcome this file has no opinion about ranks last rather than raising: a plugin is free
    # to invent one, and losing the tie is a better failure than losing the run.
    return PRECEDENCE.index(outcome) if outcome in PRECEDENCE else len(PRECEDENCE)


def _out_path(config):
    chosen = config.getoption("outcomes_out", None) or os.environ.get("OUTCOMES_OUT")
    return os.path.abspath(chosen or DEFAULT_OUT)


def pytest_sessionfinish(session, exitstatus):
    record = {
        "outcomes_version": OUTCOMES_VERSION,
        "runner": "pytest",
        "runner_version": pytest.__version__,
        "rootpath": str(session.config.rootpath),
        "exit_status": int(exitstatus),
        "collection_errors": sorted(_collection_errors),
        # Sorted, so the same suite records the same bytes whatever order its tests ran in —
        # which under xdist or a randomizing plugin is not collection order.
        "tests": dict(sorted(_outcomes.items())),
    }
    out = _out_path(session.config)
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=1, sort_keys=False)
        handle.write("\n")

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"velox-migrate: wrote {len(_outcomes)} outcomes to {out}")
