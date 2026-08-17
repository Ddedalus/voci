"""What the dump says about a suite's pytest configuration and the plugins it loaded.

`[tool.velox]` rejects unknown keys, so every setting has to be decided rather than carried over,
and the answer is worth reading before anything is converted — which is why configuration
findings are raised for the settings that translate cleanly too.

Both kinds of finding are about the suite as a whole and name no tests: a setting that has no
velox spelling blocks nothing on its own, and counting one against every test would swamp the
per-test totals that decide whether migration is affordable.
"""

from __future__ import annotations

import configparser
import tomllib
from collections.abc import Collection, Iterator
from pathlib import Path

from velox_migrate import matrix
from velox_migrate.audit.findings import Finding, Site
from velox_migrate.audit.reach import Reach
from velox_migrate.model import GroundTruth

# Values pytest reports for a setting nobody wrote, used when the suite's own config file is not
# at hand and the resolved values are all there is to go on.
_EMPTY = frozenset({"''", '""', "[]", "()", "{}", "None", "False", "0", "0.0"})


def findings(
    ground_truth: GroundTruth,
    reach: Reach,
    *,
    root: Path | None = None,
    reported_plugins: Collection[str] = (),
) -> list[Finding]:
    """Every configuration and plugin finding, in no particular order.

    `reported_plugins` are distributions whose fixtures were already classified one by one, so the
    plugin census does not repeat what the wiring findings say with more precision.
    """
    found: list[Finding] = []
    found += _ini_findings(ground_truth, root)
    found += _plugin_findings(ground_truth, reach, reported_plugins)
    return found


def _ini_findings(ground_truth: GroundTruth, root: Path | None) -> Iterator[Finding]:
    written, source = _written_settings(ground_truth, root)
    where = Site(ground_truth.inipath) if ground_truth.inipath else Site()
    for key in sorted(written):
        code = matrix.INI_SETTINGS.get(key, "VX309")
        construct = matrix.construct(code)
        value = _value(ground_truth, key)
        # "dropped" is a fate rather than a spelling, and the row's own note explains it.
        becomes = (
            f" It becomes `{construct.target}`."
            if construct.target and construct.target != "dropped"
            else ""
        )
        yield Finding(
            code=code,
            message=f"`{key}` is set{f' to {value}' if value else ''}.{becomes}",
            site=where,
            detail={"setting": key, "value": value, "read_from": source},
        )


def _written_settings(ground_truth: GroundTruth, root: Path | None) -> tuple[set[str], str]:
    """The ini keys the suite set, read from its own config file where that file is reachable.

    A key is reported under the spelling the suite wrote, not the one the extracted pytest
    registered: pytest renames its settings and keeps the old name as an alias, and a report that
    silently renamed a setting would send someone looking for a line they never wrote.

    Falling back to every registered key with a non-empty resolved value over-reports: a plugin's
    default is indistinguishable from a value someone wrote. The fallback exists so a dump carried
    out of the environment it was taken in still gets a configuration section, and each finding
    records which of the two it came from.
    """
    inipath = ground_truth.inipath
    if root is not None and inipath is not None:
        parsed = _parse_config(Path(root, inipath))
        if parsed is not None:
            return parsed, inipath
    return (
        {key for key, value in ground_truth.ini.items() if value not in _EMPTY},
        "the resolved configuration",
    )


def _parse_config(path: Path) -> set[str] | None:
    """The pytest settings written in `path`, or `None` if it holds none this can read."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if path.name == "pyproject.toml":
        try:
            loaded = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
        section = loaded.get("tool", {}).get("pytest", {}).get("ini_options")
        return set(section) if isinstance(section, dict) else None

    parser = configparser.ConfigParser()
    try:
        parser.read_string(text, source=str(path))
    except configparser.Error:
        return None
    for section in ("pytest", "tool:pytest"):
        if parser.has_section(section):
            return set(parser.options(section))
    return None


def _value(ground_truth: GroundTruth, key: str) -> str:
    """The setting's resolved value as source text, shortened to stay readable in a report."""
    try:
        value = ground_truth.ini_value(key)
    except KeyError:
        return ""
    return value if len(value) <= 80 else f"{value[:77]}..."


def _plugin_findings(
    ground_truth: GroundTruth, reach: Reach, reported: Collection[str]
) -> Iterator[Finding]:
    used_marks = {
        mark.name
        for item in ground_truth.items
        for mark in item.markers_with_origin
        if mark.name in matrix.PLUGIN_MARKS
    }
    for plugin in sorted({plugin.dist for plugin in ground_truth.plugins}):
        recipe = matrix.plugin(plugin)
        if recipe.construct.disposition is matrix.MECHANICAL or plugin in reported:
            continue
        marks = sorted(name for name in used_marks if matrix.PLUGIN_MARKS[name] == plugin)
        usage = (
            f"The suite marks tests with {', '.join(f'`{name}`' for name in marks)}."
            if marks
            else "No test requests its fixtures or carries its marks."
        )
        yield Finding(
            code=recipe.code,
            message=f"{plugin} is installed. {recipe.note} {usage}",
            detail={"plugin": plugin, "marks": ", ".join(marks)},
        )
