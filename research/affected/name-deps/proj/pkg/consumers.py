"""Consumes config.TIMEOUT via both import styles."""
from . import config
from .config import TIMEOUT


def uses_from_import():
    return TIMEOUT + 1


def uses_module_attr():
    return config.TOTAL_BUDGET
