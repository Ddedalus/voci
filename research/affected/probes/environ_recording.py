"""Swapping os.environ's class to a recording subclass sees reads through getenv, get, `in` and
missing keys -- the mechanism behind the plan's `env:` keys."""

import os

reads: list[str] = []


class Recording(type(os.environ)):
    def __getitem__(self, key):
        reads.append(key)
        return super().__getitem__(key)


os.environ.__class__ = Recording
os.getenv("HOME")
os.environ.get("PATH")
"X" in os.environ  # noqa: B015
os.environ.get("NOPE")
print(reads)  # ['HOME', 'PATH', 'X', 'NOPE']
