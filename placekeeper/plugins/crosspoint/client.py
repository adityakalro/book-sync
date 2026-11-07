"""CrossPoint authentication and saved reading positions."""
import hashlib
import re
import time

import httpx

from placekeeper.errors import ReadFailure

SERVER = 'https://sync.crosspointreader.com'
DEVICE_ID = 'placekeeper-manual'


def is_own_progress(device_id, device):
    # Existing CrossPoint markers can still carry the previous name.
    return device_id in (DEVICE_ID, 'book-sync-manual') or device in ('Placekeeper', 'Book Sync')


def crosspoint_progress(data, document):
    if data.get('document') != document:
        raise ReadFailure('CrossPoint response identifies a different document')
    xpath = data.get('progress')
    if not isinstance(xpath, str) or not re.fullmatch(
        r'/body/DocFragment\[\d+\]/body(?:/[A-Za-z][A-Za-z0-9_-]*\[\d+\])*/text\(\)\[\d+\]\.\d+', xpath
    ):
        raise ReadFailure('CrossPoint has no supported text XPointer')
    # Percentage and timestamps are diagnostic only, never position authority.
    return xpath


def read_progress(get, headers, document):
    return get('CrossPoint', f'{SERVER}/syncs/progress/{document}', headers=headers)


def write_progress(client, headers, document, xpointer, percentage):
    return client.put(SERVER + '/syncs/progress', headers=headers, json={
        'document': document, 'progress': xpointer, 'percentage': percentage,
        'device': 'Placekeeper', 'device_id': DEVICE_ID})


def authenticate(username, password):
    key = hashlib.md5(password.encode()).hexdigest()
    for attempt in range(3):
        try:
            response = httpx.get(SERVER + '/users/auth', timeout=12, follow_redirects=False,
                                 headers={'x-auth-user': username, 'x-auth-key': key})
            break
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if attempt == 2:
                raise ValueError('Could not reach CrossPoint after retrying. Check the Pi’s connection and try again.') from None
            time.sleep(0.25)
        except httpx.HTTPError:
            raise ValueError('CrossPoint did not finish responding. Try again.') from None
    if response.status_code != 200 or response.json().get('authorized') != 'OK':
        raise ValueError('CrossPoint did not accept that login.')
    return key
