"""The half of the suite written under `from __future__ import annotations`.

Every annotation here is a string at run time, which is why the extractor reads a fixture's return
type off the source rather than off `__annotations__`. Contributes the `TYPE_CHECKING`-only type
(`report`, whose `Report` exists for no line the suite executes), and the type written in a
`conftest.py` rather than imported into one (`ledger`, whose `Ledger` travels to the `fixtures.py`
this conftest becomes and is importable only from there).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from store.records import Report


class Ledger:
    def __init__(self) -> None:
        self.entries: list[str] = []


@pytest.fixture
def report() -> Report:
    from store.records import Report

    return Report(rows=1)


@pytest.fixture
def ledger() -> Ledger:
    return Ledger()
