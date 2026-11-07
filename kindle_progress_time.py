"""Read Amazon's minute-resolution furthest-read time for a document."""

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree


def parse_progress_time(xml, server_date, expected_position):
    server_time = parsedate_to_datetime(server_date).astimezone(timezone.utc)
    try:
        records = list(ElementTree.fromstring(xml).iter('last_read'))
    except ElementTree.ParseError:
        raise ValueError('Amazon returned invalid last-read XML') from None
    if len(records) != 1:
        raise ValueError('Amazon returned an ambiguous last-read record')
    record = records[0]
    if record.get('method') != 'FRL' or int(record.get('pos', '-1')) != expected_position:
        raise ValueError('Amazon last-read position changed during the check')
    message = ''.join(record.itertext())
    match = re.search(r' at (\d{1,2}):(\d{2}) (AM|PM) GMT (today|yesterday|on [A-Za-z]+ \d{1,2}, \d{4})\.', message)
    if match is None:
        raise ValueError('Amazon last-read date format is unsupported')
    hour, minute = int(match[1]), int(match[2])
    if not 1 <= hour <= 12 or not 0 <= minute < 60:
        raise ValueError('Amazon returned an invalid last-read time')
    hour = hour % 12 + (12 if match[3] == 'PM' else 0)
    if match[4].startswith('on '):
        months = 'January February March April May June July August September October November December'.split()
        month, day_number, year = match[4][3:].replace(',', '').split()
        day = datetime(int(year), months.index(month) + 1, int(day_number)).date()
    else:
        day = server_time.date() - timedelta(days=match[4] == 'yesterday')
    timestamp = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)
    if timestamp > server_time:
        raise ValueError('Amazon returned a future last-read time')
    return timestamp


def fetch_progress_time(sources, profile, sidecar, position):
    response = sources.client.get(
        'https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/getAnnotations',
        auth=sources.auth,
        params={'filter': 'last_read', 'type': 'PDOC', 'key': profile['document'],
                'guid': sidecar['payload']['guid']},
    )
    if response.status_code != 200:
        raise ValueError(f'Amazon last-read time returned HTTP {response.status_code}')
    return parse_progress_time(response.content, response.headers['date'], position)
