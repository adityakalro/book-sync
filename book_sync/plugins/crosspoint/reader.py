"""CrossPoint progress in the verified EPUB's text coordinates."""
from contextlib import contextmanager
import json

import httpx

from book_sync.errors import MissingProgress, ReadFailure
from book_sync.mapping.text import epub_index, epub_position_details
from book_sync.plugins.contracts import Observation
from book_sync.plugins.transport import check_write, json_reader
from .client import DEVICE_ID, crosspoint_progress, read_progress, write_progress


class CrossPointReader:
    prefer_recent = False

    def __init__(self, client, headers, document, epub, coordinates):
        self.client, self.headers, self.document = client, headers, document
        self.epub, self.coordinates = epub, coordinates
        self.get = json_reader(client, missing_status=404)

    def read(self, hint):
        data = read_progress(self.get, self.headers, self.document)
        if not data:
            raise MissingProgress('X4 has no saved position for this book.')
        native = crosspoint_progress(data, self.document)
        stamp = data.get('timestamp')
        if type(stamp) in (int, float) and stamp >= 100_000_000_000:
            stamp /= 1000
        if type(stamp) not in (int, float) or stamp <= 0:
            if hint['need_timestamp']:
                raise ReadFailure('X4 returned no valid progress timestamp.')
            stamp = None
        own = data.get('device_id') == DEVICE_ID or data.get('device') == 'Book Sync'
        return Observation(native, self.coordinates.at_or_after(native), stamp, own=own)

    def equivalent(self, first, second):
        return first == second

    def target(self, anchor, current):
        return self.coordinates[anchor]

    def write(self, target):
        percentage = epub_position_details(self.epub, target)['percentage']
        check_write(write_progress(self.client, self.headers, self.document, target, percentage), 'X4')


@contextmanager
def open_reader(root, book):
    binding = book.bindings.get('crosspoint')
    if not binding or book.epub is None:
        yield None
        return
    credentials = json.loads((root/'crosspoint-auth.json').read_text())
    headers = {'x-auth-user': credentials['username'], 'x-auth-key': credentials['auth_key']}
    coordinates = book.mapper.epub.coordinates if book.mapper else epub_index(book.epub).coordinates
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        yield CrossPointReader(client, headers, binding, book.epub, coordinates)
