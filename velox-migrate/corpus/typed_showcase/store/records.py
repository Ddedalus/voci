"""A type `store/conftest.py` names only inside `if TYPE_CHECKING:`."""

from dataclasses import dataclass


@dataclass
class Report:
    rows: int
