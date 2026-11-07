"""Execute plans against an arbitrary set of reader sessions."""
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
from time import time

from book_sync.errors import MissingProgress, ReadFailure
from book_sync.storage import private_json
from book_sync.sync.planner import plan


def observe(readers, state):
    positions = {}
    need_timestamp = any(getattr(reader, 'prefer_recent', False) for reader in readers.values())
    for key, reader in readers.items():
        pending = state.get('pending') or {}
        hint = {'need_timestamp': need_timestamp, 'receipt': state['receipts'].get(key),
                'pending': pending if pending.get('device') == key else None}
        try:
            positions[key] = reader.read(hint)
        except MissingProgress:
            continue
    return positions


def receipt(observation):
    return {'position': observation.native, 'stamp': observation.stamp}


def reconcile(state, observations):
    pending = state.get('pending')
    if not pending:
        return
    key = pending['device']
    current = observations.get(key)
    if current is None or receipt(current) == pending['before']:
        raise ReadFailure('An earlier write is unconfirmed. Check that destination before syncing again.')
    if current.native == pending['target']:
        state['receipts'][key] = receipt(current)
    state.pop('pending')


def execute(readers, state, path, dry_run=False):
    writes = 0
    outcomes = {}
    try:
        positions = observe(readers, state)
        if state.get('pending'):
            reconcile(state, positions)
            if not dry_run:
                private_json(path, state)
        source, targets, message = plan(readers, positions, state['receipts'])
        if source is None:
            return {'status': 'skipped' if len(positions) < 2 else 'in_sync',
                    'writes': 0, 'message': message}
        if dry_run:
            return {'status': 'preview', 'writes': 0, 'source': source, 'targets': targets,
                    'message': 'Preview from ' + source}
        outcomes.update({key: 'kept' for key in positions if key != source and key not in targets})
        for key, target in targets.items():
            fresh = observe(readers, state)
            if fresh != positions:
                return {'status': 'changed', 'writes': writes, 'outcomes': outcomes,
                        'message': 'A reader changed during sync. Try again.'}
            state['pending'] = {'device': key, 'target': target,
                                'before': receipt(fresh[key]), 'stamp': time()}
            private_json(path, state)
            writes += 1
            readers[key].write(target)
            after = observe(readers, state)
            if key not in after or after[key].native != target:
                raise ReadFailure(f'{key} saved position could not be verified.')
            state['receipts'][key] = receipt(after[key])
            state.pop('pending')
            private_json(path, state)
            outcomes[key] = 'verified'
            if {k: v for k, v in after.items() if k != key} != {k: v for k, v in fresh.items() if k != key}:
                return {'status': 'changed', 'writes': writes, 'outcomes': outcomes,
                        'message': 'Another reader changed during sync. Try again.'}
            positions = after
        verified = [key for key, result in outcomes.items() if result == 'verified']
        return {'status': 'synced' if writes else 'in_sync', 'writes': writes, 'source': source,
                'outcomes': outcomes,
                'message': f'{source} → {", ".join(verified)} verified'
                if writes else 'The selected position required no supported updates.'}
    except Exception as error:
        return {'status': 'unconfirmed' if writes else 'error', 'writes': writes, 'outcomes': outcomes,
                'message': str(error) if isinstance(error, ReadFailure) else 'Could not complete this book’s sync.'}


def sync_book(root, book, plugins, dry_run=False):
    path = Path(root) / 'progress' / (sha256(book.key.encode()).hexdigest() + '.json')
    if path.exists():
        state = json.loads(path.read_text())
    else:
        state = book.initial_state() if book.initial_state else {'receipts': {}}
    if state.get('edition', book.edition) != book.edition:
        if state.get('pending'):
            raise ReadFailure('An unconfirmed write belongs to an earlier edition map.')
        state = {'receipts': {}}
    state['edition'] = book.edition
    with ExitStack() as stack:
        readers = {}
        for key, plugin in plugins.items():
            if plugin.reader is None:
                continue
            try:
                session = stack.enter_context(plugin.reader(root, book))
            except MissingProgress:
                continue
            if session is not None:
                readers[key] = session
        return execute(readers, state, path, dry_run)
