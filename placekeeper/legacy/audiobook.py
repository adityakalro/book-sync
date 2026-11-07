"""Timestamp-selected three-way progress updates for explicit Sync now jobs."""
from datetime import datetime, timezone
import json
from pathlib import Path
from xml.etree.ElementTree import ParseError

import httpx
from placekeeper.mapping.chapters import ChapterMap
from placekeeper.plugins.kindle.progress import fetch_progress_time, write_position
from placekeeper.plugins.libby.client import elapsed
from placekeeper.plugins.libby.reader import AUDIO_TOLERANCE_MS
from placekeeper.errors import MissingProgress, ReadFailure
from placekeeper.plugins.crosspoint.client import is_own_progress, write_progress
from placekeeper.storage import private_json
from placekeeper.mapping.text import epub_position_details


def timestamp(value):
    if type(value) not in (int, float) or value <= 0:
        raise ReadFailure('A reader returned no valid progress timestamp.')
    return datetime.fromtimestamp(value / 1000 if value >= 100_000_000_000 else value, timezone.utc)


def select_source(records, receipts):
    candidates = []
    for device, record in records.items():
        receipt = receipts.get(device)
        if receipt and receipt['position'] == record['position'] and receipt['stamp'] == record['stamp']:
            continue
        if record.get('own'):
            continue
        candidates.append(device)
    candidates.sort(key=lambda device: records[device]['stamp'], reverse=True)
    if not candidates:
        return None
    if len(candidates) > 1 and records[candidates[0]]['stamp'] - records[candidates[1]]['stamp'] < 60:
        raise ReadFailure('Reader timestamps are within one minute. Sync the reader you want to use, then try again.')
    return candidates[0]


def read_records(sources, profile, libby, loan, timing, state, marker=None):
    snapshot, kindle, cp = sources.details(allow_missing=True)
    if marker is None:
        marker = libby.position(loan)
    records = {
        'Libby': {'position': elapsed(timing, marker),
                  'stamp': timestamp(marker.get('timestamp')).timestamp(),
                  'own': marker.get('deviceId') == libby.state.get('chipId')},
    }
    if snapshot.kindle_position is not None:
        receipt = state['receipts'].get('Kindle')
        pending = state.get('pending', {})
        own = True
        if receipt and receipt['position'] == snapshot.kindle_position:
            stamp = receipt['stamp']
        elif pending.get('device') == 'Kindle' and pending['target'] == snapshot.kindle_position:
            stamp = pending.get('stamp', pending['before']['stamp'])
        else:
            stamp = fetch_progress_time(sources, profile, kindle, snapshot.kindle_position).timestamp()
            own = False
        records['Kindle'] = {'position': snapshot.kindle_position, 'stamp': stamp, 'own': own}
    if snapshot.crosspoint_xpointer:
        records['X4'] = {'position': snapshot.crosspoint_xpointer,
                         'stamp': timestamp(snapshot.crosspoint_timestamp).timestamp(),
                         'own': is_own_progress(cp.get('device_id'), snapshot.crosspoint_device)}
    return records, kindle


def write_target(device, target, sources, profile, kindle, libby, loan, player, url, timing, epub):
    if device == 'Libby':
        libby.write(player, url, timing, target)
        return
    if device == 'X4':
        fallback = epub_position_details(epub, target)
        response = write_progress(sources.client, sources.headers, profile['crosspoint_document'],
                                  target, fallback['percentage'])
    else:
        response = write_position(sources.client, sources.auth, profile['document'],
                                  kindle['payload'].get('guid'), target)
    if response.status_code != 200:
        raise ReadFailure(f'{device} update returned HTTP {response.status_code}.')


def apply_three_way(profile, mapper, sources, directory, libby, loan, dry_run=False):
    directory = Path(directory)
    state_path = directory / 'three-way-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'receipts': {}}
    player = None
    writes = 0
    outcomes = []
    try:
        # Kindle can omit the saved time after an ebook-only Placekeeper write.
        # Reuse the verified receipt when an audiobook later becomes available.
        ebook_result = directory / 'result.json'
        if ebook_result.exists():
            saved = json.loads(ebook_result.read_text())
            result = saved.get('result', {})
            if result.get('status') == 'synced' and result.get('direction') == 'crosspoint_to_kindle':
                stamp = datetime.fromisoformat(saved['finished_at']).timestamp()
                if stamp > state['receipts'].get('Kindle', {}).get('stamp', 0):
                    state['receipts']['Kindle'] = {'position': saved['readback']['kindle_position'], 'stamp': stamp}
        try:
            marker = libby.position(loan)
        except MissingProgress as error:
            if state.get('pending'):
                raise
            return {'status': 'unavailable', 'writes': 0, 'message': str(error)}
        player, url, timing = libby.open_player(loan)
        epub = Path(profile['files']['epub']['path'])
        if not epub.is_absolute():
            raise ReadFailure('Three-way sync requires an absolute EPUB path.')
        chapters = ChapterMap(epub, mapper.epub.coordinates, timing)
        records, kindle = read_records(sources, profile, libby, loan, timing, state, marker)
        # Reconcile an interrupted write before selecting a source or issuing another write.
        pending = state.get('pending')
        if pending:
            record = records.get(pending['device'])
            if record and record['position'] == pending['target']:
                state['receipts'][pending['device']] = record
            elif record == pending['before']:
                raise ReadFailure('An earlier update has an uncertain outcome. Recheck the destination before syncing again.')
            state.pop('pending')
            if not dry_run:
                private_json(state_path, state)
        if len(records) < 2:
            return {'status': 'skipped', 'writes': 0,
                    'message': 'Sync this book on Kindle or X4 first so it has a saved reading position.'}
        source = select_source(records, state['receipts'])
        if source is None:
            return {'status': 'in_sync', 'message': 'All saved positions came from Placekeeper. Read or listen, then sync again.', 'writes': 0}
        if source == 'Libby':
            source_audio = records[source]['position']
            coordinate, label = chapters.to_text(source_audio)
        else:
            coordinate = (mapper.from_sidecar(records[source]['position'])['xpointer']
                          if source == 'Kindle' else records[source]['position'])
            source_audio, label = chapters.to_audio(coordinate)
        targets = {'X4': coordinate, 'Libby': source_audio}
        if 'Kindle' in records:
            targets['Kindle'] = mapper.from_xpointer(coordinate)['suggested_forward_position']
        # The observation makes the selected source and proposed targets inspectable privately.
        if not dry_run:
            private_json(directory / 'three-way-observation.json',
                         {'source': source, 'chapter': label, 'records': records, 'targets': targets})
        expected = records
        for device in ('X4', 'Kindle', 'Libby'):
            if device == source or device not in records:
                continue
            target = targets[device]
            current = records[device]['position']
            if device == 'Kindle':
                same = abs(current - target) < 150
                if current > target and not same:
                    outcomes.append('Kindle is ahead of the selected source; its furthest-read marker was kept')
                    continue
            elif device == 'X4':
                same = current == target
                if not same and source == 'Libby':
                    try:
                        current_audio, _ = chapters.to_audio(current)
                    except ValueError:
                        # The destination can be in front matter outside the audio map.
                        pass
                    else:
                        same = abs(current_audio - source_audio) <= AUDIO_TOLERANCE_MS
                elif not same:
                    same = abs(mapper.from_xpointer(current)['suggested_forward_position']
                               - targets['Kindle']) < 150
            else:
                same = abs(current - target) <= AUDIO_TOLERANCE_MS
            if same:
                outcomes.append(device + ' already near the selected passage')
                continue
            if dry_run:
                outcomes.append(device + ' would update')
                continue
            fresh, kindle = read_records(sources, profile, libby, loan, timing, state)
            if fresh != expected:
                return {'status': 'changed', 'writes': writes, 'source': source,
                        'message': 'Progress changed during sync. ' + '. '.join(outcomes)}
            state['pending'] = {'device': device, 'target': target, 'before': fresh[device],
                                'stamp': datetime.now(timezone.utc).timestamp()}
            private_json(state_path, state)
            writes += 1
            write_target(device, target, sources, profile, kindle, libby, loan, player, url, timing, epub)
            after, _ = read_records(sources, profile, libby, loan, timing, state)
            if after.get(device, {}).get('position') != target:
                return {'status': 'unconfirmed', 'writes': writes, 'source': source,
                        'message': device + ' accepted the update, but its saved position could not be verified.'}
            state['receipts'][device] = after[device]
            state.pop('pending')
            private_json(state_path, state)
            if {k: v for k, v in after.items() if k != device} != {k: v for k, v in fresh.items() if k != device}:
                return {'status': 'changed', 'writes': writes, 'source': source,
                        'message': device + ' updated, but another reader changed during sync. Try again.'}
            expected = after
            outcomes.append(device + ' updated and verified')
        return {'status': 'synced' if writes else 'in_sync', 'writes': writes, 'source': source,
                'message': f'{source} → {label}. ' + '. '.join(outcomes)}
    except (ReadFailure, ValueError, KeyError, TypeError, ParseError, StopIteration, OSError, httpx.HTTPError) as error:
        message = str(error) if isinstance(error, ReadFailure) else 'Three-way sync could not finish. Recheck connections and chapter matching.'
        return {'status': 'unconfirmed' if writes else 'error', 'writes': writes,
                'message': '. '.join(outcomes + [message])}
    finally:
        if player:
            player.close()
