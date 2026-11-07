"""Discover trusted plugins installed under book_sync/plugins."""
from importlib import import_module
import pkgutil
import re

from book_sync import plugins


def installed():
    registry = {}
    for module in sorted(pkgutil.iter_modules(plugins.__path__), key=lambda item: item.name):
        if not module.ispkg:
            continue
        spec = import_module(f'{plugins.__name__}.{module.name}.plugin').PLUGIN
        if not re.fullmatch(r'[a-z][a-z0-9_-]*', spec.id) or spec.id in registry:
            raise ValueError('Invalid or duplicate plugin ID')
        registry[spec.id] = spec
    return registry
