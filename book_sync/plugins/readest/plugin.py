"""Readest content-provider registration."""
from book_sync.plugins.contracts import Plugin
from book_sync.plugins.kindle.legacy_catalog import invalidate
from .client import pairing_finish, pairing_start

FILE = 'readest-device.json'


def setup(root, pending, action, data):
    if action == 'start':
        return pairing_start(root/FILE, pending)
    pairing_finish(root/FILE, pending)
    return {'connected': True}


PLUGIN = Plugin('readest', 'Readest', (FILE,),
    'Link your cloud library to supply matching EPUBs.',
    actions=({'id': 'start', 'label': 'Get Readest code'},
             {'id': 'finish', 'label': 'Check approval', 'after': 'start'}),
    setup=setup, disconnect=invalidate)
