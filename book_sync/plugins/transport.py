"""Shared read-only HTTP handling. Progress writes are never retried."""
import time
import httpx

from book_sync.errors import MissingProgress, ReadFailure


def json_reader(client, missing_status=None):
    def get(service, url, **kwargs):
        for attempt in range(3):
            try:
                response = client.get(url, **kwargs)
                break
            except httpx.HTTPError:
                if attempt == 2:
                    raise ReadFailure(f'{service} connection failed.') from None
                time.sleep(.25)
        if response.status_code == missing_status:
            raise MissingProgress(f'{service} has no saved position for this book.')
        if response.status_code != 200:
            raise ReadFailure(f'{service} read failed: HTTP {response.status_code}.')
        try:
            data = response.json()
        except ValueError:
            raise ReadFailure(f'{service} returned invalid progress data.') from None
        if not isinstance(data, dict):
            raise ReadFailure(f'{service} returned invalid progress data.')
        return data
    return get


def check_write(response, service):
    if response.status_code != 200:
        raise ReadFailure(f'{service} update returned HTTP {response.status_code}.')
