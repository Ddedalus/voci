"""Dynamic import by runtime-computed name -- expected gap."""
import importlib


def load_plugin(name):
    mod = importlib.import_module(f"pkg.plugins.{name}")
    return mod.VALUE
