"""A deliberately dynamic registry: static navigation cannot select a target."""

from importlib import import_module


def load_function(path):
    module, _, attribute = path.rpartition(".")
    return getattr(import_module(module), attribute)
