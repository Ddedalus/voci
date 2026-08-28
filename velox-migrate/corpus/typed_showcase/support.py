"""The types the root conftest's fixtures are annotated with, written outside any conftest.

A type a conftest *imports* is one the conversion repeats the import of rather than re-exporting
through the `fixtures.py` the conftest becomes, so this module is what that case needs.
"""


class Session:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn


class Widget:
    def __init__(self, name: str) -> None:
        self.name = name
