"""How much of a suite's fixture typing survives conversion, counted per fixture.

An injected parameter's type comes from the fixture factory's return annotation and from nowhere
else: conversion carries across whatever the suite already states and invents nothing, so a
factory whose annotation says nothing loses the type at every parameter it is injected into. The
count per fixture is how many of those there are, which is what makes the list a worklist —
annotating the fixture at the top of it retypes the most code.

"Says nothing" is `inference.for_factory`'s answer, the same call the conversion makes, so this
list and the conversion report name the same fixtures: a factory annotated `-> Any` is on it,
because `Any` is exactly as much as no annotation at all and exactly as much work to fix.

Sites come from the dump's resolved chains, so a name is charged to the definition pytest picked
for the test requesting it rather than to every definition that shares the name.
"""

from __future__ import annotations

from pathlib import Path

from velox_migrate.audit.findings import Site, TypeReadiness, Unannotated
from velox_migrate.audit.wiring import _SYNTHETIC_PREFIXES, in_suite
from velox_migrate.inference import factory, for_factory, infer
from velox_migrate.model import FixtureDef, GroundTruth


def assess(ground_truth: GroundTruth, *, root: Path | None = None) -> TypeReadiness:
    """The suite's fixtures whose return annotation states no type, worst cost first.

    `root` is where the suite's sources are, which the annotation is read *against*: an aliased
    `import typing as t` is what makes `-> t.Any` the nothing it is. Without one the answer is
    taken from the annotation alone, and a suite whose sources cannot be read is listed by what
    its dump says rather than not listed at all.
    """
    sources = _Sources(root)
    sites = _injection_sites(ground_truth)
    written = [fixture for fixture in ground_truth.fixture_defs.values() if _written(fixture)]
    bare = [fixture for fixture in written if not _states_a_type(fixture, sources)]
    rows = [
        Unannotated(
            argname=fixture.argname,
            module=fixture.func.module,
            site=Site(
                file=fixture.func.file, line=fixture.func.lineno, function=fixture.func.qualname
            ),
            injections=len(sites.get(fixture.key, ())),
        )
        for fixture in bare
    ]
    return TypeReadiness(
        fixtures=tuple(sorted(rows, key=lambda row: row.sort_key)),
        annotated=len(written) - len(bare),
    )


class _Sources:
    """The suite's files under `root`, read once each and remembered.

    Several fixtures share a `conftest.py`, and the annotation of each is read against the whole
    module's imports, so the file would otherwise be read once per fixture written in it.
    """

    def __init__(self, root: Path | None) -> None:
        self._root = root
        self._text: dict[str, str | None] = {}

    def of(self, file: str | None) -> str | None:
        if self._root is None or file is None:
            return None
        if file not in self._text:
            path = Path(self._root, file)
            try:
                self._text[file] = path.read_text(encoding="utf-8")
            except OSError:
                self._text[file] = None
        return self._text[file]


def _states_a_type(fixture: FixtureDef, sources: _Sources) -> bool:
    """Whether this factory's annotation gives an injected parameter anything to be checked as.

    Asked of the annotation rather than of its presence, so `-> Any` counts as the nothing it is,
    and asked through `inference.for_factory` with the factory's own definition and module in
    hand — the same call the conversion makes, so this worklist and the conversion report cannot
    name different fixtures.
    """
    text = sources.of(fixture.func.file)
    node = factory(text, fixture.func.qualname) if text is not None else None
    if text is None or node is None:
        return infer(fixture.returns) is not None
    return for_factory(fixture.returns, node, text, fixture.func.file or "") is not None


def _injection_sites(ground_truth: GroundTruth) -> dict[str, set[tuple[str, str]]]:
    """Every parameter that receives a fixture, as `(the function it is written in, its name)`.

    A test function is one entry however many cases it was parametrized into, since the parameter
    is written once; a fixture factory's own requests are entries too, since conversion injects
    those the same way.
    """
    found: dict[str, set[tuple[str, str]]] = {}
    for item in ground_truth.items:
        qualname = f"{item.cls}.{item.originalname}" if item.cls else item.originalname
        owner = f"{item.path}::{qualname}"
        for name in item.argnames:
            requested = item.resolve(name)
            if requested is not None:
                found.setdefault(requested.key, set()).add((owner, name))
        for fixture, deps in item.edges():
            if not in_suite(fixture):
                continue
            for edge in deps:
                if edge.fixture is not None:
                    found.setdefault(edge.fixture.key, set()).add((fixture.key, edge.name))
    return found


def _written(fixture: FixtureDef) -> bool:
    """Whether this definition is a factory the suite wrote, and could annotate."""
    return (
        in_suite(fixture)
        and not fixture.direct_param
        and not fixture.argname.startswith(_SYNTHETIC_PREFIXES)
    )
