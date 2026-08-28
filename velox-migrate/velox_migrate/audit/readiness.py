"""How much of a suite's fixture typing survives conversion, counted per fixture.

An injected parameter's type comes from the fixture factory's return annotation and from nowhere
else: conversion carries across whatever the suite already states and invents nothing, so a
factory whose annotation says nothing loses the type at every parameter it is injected into. The
count per fixture is how many of those there are, which is what makes the list a worklist —
annotating the fixture at the top of it retypes the most code.

"Says nothing" is `inference.infer`'s own answer and not a second opinion, so this list and the
conversion report name the same fixtures: a factory annotated `-> Any` is on it, because `Any` is
exactly as much as no annotation at all and is exactly as much work to fix.

Sites come from the dump's resolved chains, so a name is charged to the definition pytest picked
for the test requesting it rather than to every definition that shares the name.
"""

from __future__ import annotations

from velox_migrate.audit.findings import Site, TypeReadiness, Unannotated
from velox_migrate.audit.wiring import in_suite
from velox_migrate.inference import infer
from velox_migrate.model import FixtureDef, GroundTruth

# pytest's own wrappers around a class lifecycle, and the fixture `@parametrize` desugars to:
# neither is a factory anyone wrote a signature for.
_SYNTHETIC_PREFIXES = ("_xunit_", "_unittest_")


def assess(ground_truth: GroundTruth) -> TypeReadiness:
    """The suite's fixtures whose return annotation states no type, worst cost first."""
    sites = _injection_sites(ground_truth)
    written = [fixture for fixture in ground_truth.fixture_defs.values() if _written(fixture)]
    bare = [fixture for fixture in written if not _states_a_type(fixture)]
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


def _states_a_type(fixture: FixtureDef) -> bool:
    """Whether this factory's annotation gives an injected parameter anything to be checked as.

    Asked of the annotation rather than of its presence, so `-> Any` counts as the nothing it is.
    The unwrapping arguments are left at their defaults: whether the factory is a generator
    decides *which* type comes out, never whether one does, and reading its body to find out would
    cost the audit a source read per fixture for an answer it does not use.
    """
    return infer(fixture.returns) is not None


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
        for fixture in item.walk():
            if not in_suite(fixture):
                continue
            for edge in item.dependencies(fixture):
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
