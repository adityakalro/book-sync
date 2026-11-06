"""Read-only Kindle/CrossPoint poller. There is deliberately no write client."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import getpass
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import itertools
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from urllib.parse import parse_qs

from text_mapping import Mapper


ROOT = Path(__file__).resolve().parent
SERVER = 'https://sync.crosspointreader.com'


def private_json(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def append_event(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'a') as f:
        f.write(json.dumps(data, separators=(',', ':'))+'\n')


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    kindle_position: int
    crosspoint_xpointer: str
    crosspoint_device: str = ''
    crosspoint_timestamp: int | float | None = None


class ReadFailure(Exception):
    """An error safe to show without request headers, tokens or response bodies."""


def kindle_position(data, document):
    payload = data.get('payload', {})
    if not isinstance(payload, dict) or not isinstance(payload.get('records'), list):
        raise ReadFailure('Kindle returned an unexpected progress structure')
    if payload.get('key') != document or payload.get('type') != 'PDOC':
        raise ReadFailure('Kindle response identifies a different document or type')
    values = []
    for record in payload.get('records', []):
        if not isinstance(record, dict):
            raise ReadFailure('Kindle returned an unexpected progress record')
        if record.get('type') == 'kindle.lpr':
            value = record.get('location')
            if not re.fullmatch(r'\d+', str(value)):
                raise ReadFailure('Kindle returned an invalid last-read position')
            values.append(int(value))
    if not values:
        raise ReadFailure('Kindle returned no last-read position')
    return max(values)


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


class ReadOnlySources:
    def __init__(self, amazon_auth, username, auth_key, kindle_document, crosspoint_document, client=None):
        import httpx
        from audible import Authenticator
        self.client = client or httpx.Client(timeout=20, follow_redirects=False)
        self.auth = Authenticator.from_dict(json.loads(Path(amazon_auth).read_text()))
        self.headers = {'x-auth-user': username, 'x-auth-key': auth_key}
        self.kindle_document = kindle_document
        self.crosspoint_document = crosspoint_document

    def _get(self, service, url, **kwargs):
        import httpx
        try:
            response = self.client.get(url, **kwargs)
        except httpx.HTTPError:
            raise ReadFailure(f'{service} connection failed') from None
        if response.status_code != 200:
            raise ReadFailure(f'{service} read failed: HTTP {response.status_code}')
        try:
            data = response.json()
        except ValueError:
            raise ReadFailure(f'{service} returned invalid JSON') from None
        if not isinstance(data, dict):
            raise ReadFailure(f'{service} returned an unexpected JSON structure')
        return data

    def read(self):
        kindle = self._get('Kindle', 'https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/sidecar',
                           auth=self.auth, params={'type': 'PDOC', 'key': self.kindle_document})
        cp = self._get('CrossPoint', f'{SERVER}/syncs/progress/{self.crosspoint_document}', headers=self.headers)
        return Snapshot(kindle_position(kindle, self.kindle_document),
                        crosspoint_progress(cp, self.crosspoint_document),
                        str(cp.get('device', '')), cp.get('timestamp'))

    def close(self):
        self.client.close()


class DryRunEngine:
    def __init__(self, mapper, profile_id, state=None):
        self.mapper = mapper
        self.profile_id = profile_id
        self.state = state or {'profile_id': profile_id, 'previous': None, 'pending': None}
        if self.state.get('profile_id') != profile_id:
            raise ValueError('State belongs to a different book/map; use a new state file')
        self.state = json.loads(json.dumps(self.state))

    def observe(self, snapshot):
        previous = self.state['previous']
        # Resolve both directions first. An unmappable position must not advance
        # the checkpoint or leave a partially prepared proposal.
        cp = self.mapper.from_xpointer(snapshot.crosspoint_xpointer)
        kindle = self.mapper.from_sidecar(snapshot.kindle_position)
        low, high = cp['sidecar_range']
        changes = [] if previous is None else [name for name, field in (
            ('kindle', 'kindle_position'), ('crosspoint', 'crosspoint_xpointer'))
            if previous[field] != getattr(snapshot, field)]
        pending = self.state['pending']
        event = {'mode': 'dry-run', 'writes_performed': 0,
                 'observation': asdict(snapshot), 'changed': changes,
                 'baseline': previous is None, 'policy': 'forward-only',
                 'mapping': {'crosspoint': cp, 'kindle': kindle}}
        if low <= snapshot.kindle_position <= high:
            event['status'] = 'destination_caught_up' if pending else 'in_sync'
            if pending:
                event['resolved_proposal_id'] = pending['id']
            next_pending = None
        else:
            if snapshot.kindle_position < low:
                proposal = {'direction': 'crosspoint_to_kindle',
                            'source_xpointer': snapshot.crosspoint_xpointer,
                            'target_sidecar_position': cp['suggested_forward_position'],
                            'expected_target_range': cp['sidecar_range']}
            else:
                proposal = {'direction': 'kindle_to_crosspoint',
                            'source_sidecar_position': snapshot.kindle_position,
                            'target_xpointer': kindle['xpointer']}
            # Include both observed positions: renewed destination movement is
            # a new decision, even if the proposed target remains the same.
            proposal['id'] = digest({'profile': self.profile_id, 'proposal': proposal,
                                     'kindle': snapshot.kindle_position,
                                     'crosspoint': snapshot.crosspoint_xpointer})
            event['status'] = 'proposal_unchanged' if pending and pending['id'] == proposal['id'] else 'would_sync'
            event['proposal'] = proposal
            next_pending = proposal
        self.state = {'profile_id': self.profile_id, 'previous': asdict(snapshot), 'pending': next_pending}
        return event


def load_profile(path):
    path = Path(path).resolve()
    profile = json.loads(path.read_text())
    if not re.fullmatch(r'[A-Z0-9]{10,64}', profile['document']):
        raise ValueError('Invalid Kindle document identifier')
    if not re.fullmatch(r'[a-f0-9]{32}', profile['crosspoint_document']):
        raise ValueError('Invalid CrossPoint document identifier')
    files = {}
    for name in ('epub', 'content', 'locations'):
        record = profile['files'][name]
        # Existing profiles store paths relative to the project, one level above
        # their private directory. Absolute paths also support moving a bundle.
        source = Path(record['path'])
        if not source.is_absolute():
            source = path.parent.parent/source
        if hashlib.sha256(source.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError(f'{name} fingerprint differs from the validated book map')
        files[name] = source
    mapper = Mapper(**files)
    if profile.get('schema_version') == 2:
        from onboarding import evaluate_pair, validate_map
        if (profile.get('status') != 'ready_for_dry_run'
                or profile.get('automatic_progress_writes_enabled') is not False
                or profile.get('bridge',{}).get('model') != 'legacy_sidecar_location_bucket_150'
                or hashlib.md5(profile['x4_filename'].encode()).hexdigest() != profile['crosspoint_document']):
            raise ValueError('Automatic profile has an unsupported identity or sync policy')
        if not evaluate_pair(files['epub'],files['content'])['accepted']:
            raise ValueError('Automatic profile text alignment failed')
        if not validate_map(mapper)['accepted']:
            raise ValueError('Automatic profile coordinate checks failed')
        return profile, mapper, digest(profile)
    highlight = profile['highlight_check']
    handoff = profile['physical_handoff_check']
    actual = mapper.from_xpointer(highlight['predicted']['xpointer'])
    low, high = actual['sidecar_range']
    if not highlight.get('passed') or not low <= highlight['observed_sidecar_start'] <= high:
        raise ValueError('Highlight calibration check failed')
    actual = mapper.from_xpointer(handoff['predicted']['xpointer'])
    if not handoff.get('passed') or actual['suggested_forward_position'] != handoff['observed_successful_sidecar_write']:
        raise ValueError('Physical handoff calibration check failed')
    return profile, mapper, digest(profile)


@contextmanager
def state_lock(path):
    # Supported on Linux (Pi) and macOS. Lock a separate inode so atomic state
    # replacement never permits a second poller to race this process.
    import fcntl
    fd = os.open(str(path)+'.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another loop is using this state file') from None
        yield
    finally:
        os.close(fd)


def browser_login(port, username):
    """Collect a password once, keep only its API hash in process memory."""
    import html
    origin = f'http://127.0.0.1:{port}'
    csrf = secrets.token_urlsafe(32)
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, body):
            data = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'none'; form-action 'self'; frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{port}' or self.path != '/':
                self.respond(404, 'Unknown page')
                return
            self.respond(200, f'''<!doctype html><meta charset="utf-8">
                <title>Book Sync dry-run login</title><h1>Read-only CrossPoint test</h1>
                <p>This loop will only read progress from CrossPoint and Kindle.</p>
                <p>Your sync password and its API hash stay in process memory and are not saved.</p>
                <form method="post" action="/login">
                <input type="hidden" name="csrf" value="{csrf}">
                <p><label>Sync username <input name="username" required autocomplete="off"
                value="{html.escape(username, quote=True)}"></label></p>
                <p><label>Sync password <input name="password" type="password" required
                autocomplete="off"></label></p><button>Start read-only test</button></form>''')

        def do_POST(self):
            if (self.path != '/login' or self.headers.get('Host') != f'127.0.0.1:{port}'
                    or self.headers.get('Origin') != origin):
                self.respond(403, 'Invalid origin or path')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                length = 0
            if not 0 < length <= 8192:
                self.respond(400, 'Invalid request size')
                return
            try:
                fields = parse_qs(self.rfile.read(length).decode())
            except UnicodeError:
                self.respond(400, 'Invalid form encoding')
                return
            if not secrets.compare_digest(fields.get('csrf', [''])[0], csrf):
                self.respond(403, 'Invalid session')
                return
            user = fields.get('username', [''])[0].strip()
            password = fields.get('password', [''])[0]
            if not user or not password:
                self.respond(400, 'Both fields are required')
                return
            result.update(username=user, auth_key=hashlib.md5(password.encode()).hexdigest())
            self.respond(200, '<h1>Read-only test starting</h1><p>No reading progress will be written.</p>')

    server = HTTPServer(('127.0.0.1', port), Handler)
    server.timeout = 1
    print(json.dumps({'status': 'awaiting_crosspoint_login', 'url': origin}), flush=True)
    try:
        while not result:
            server.handle_request()
    finally:
        server.server_close()
    return result['username'], result['auth_key']


def run_loop(engine, read, state_path, log_path, *, count=3, interval=15):
    """A failed/partial observation is logged, but never committed as a baseline."""
    failures = 0
    for cycle in range(count) if count else itertools.count():
        stamp = datetime.now(timezone.utc).isoformat()
        before = json.loads(json.dumps(engine.state))
        try:
            event = engine.observe(read())
        except (ReadFailure, ValueError) as error:
            failures += 1
            event = {'mode': 'dry-run', 'status': 'observation_failed',
                     'error': str(error), 'writes_performed': 0}
        event.update(observed_at=stamp, cycle=cycle+1)
        # Log first. If persistence fails, stop; do not carry unrecorded state.
        try:
            append_event(log_path, event)
            if event['status'] != 'observation_failed':
                private_json(state_path, engine.state)
        except OSError:
            engine.state = before
            raise
        print(json.dumps(event), flush=True)
        if count == 0 or cycle+1 < count:
            time.sleep(interval)
    return 1 if failures else 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', type=Path, required=True, help='Validated profile from the private onboarding registry')
    p.add_argument('--amazon-auth', type=Path, default=ROOT/'.private/amazon-device.json')
    p.add_argument('--state', type=Path, default=ROOT/'.private/dry-run-state.json')
    p.add_argument('--log', type=Path, default=ROOT/'.private/dry-run-events.jsonl')
    p.add_argument('--count', type=int, default=3, help='Poll count; 0 means keep running')
    p.add_argument('--interval', type=float, default=15)
    p.add_argument('--login', choices=['browser', 'terminal'], default='browser')
    p.add_argument('--port', type=int, default=8766)
    p.add_argument('--username', default='')
    args = p.parse_args()
    if args.count < 0 or args.interval < 1:
        p.error('count must be nonnegative and interval at least one second')
    os.umask(0o077)
    try:
        profile, mapper, profile_id = load_profile(args.profile)
        args.state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with state_lock(args.state):
            state = json.loads(args.state.read_text()) if args.state.exists() else None
            engine = DryRunEngine(mapper, profile_id, state)
            if args.login == 'browser':
                username, auth_key = browser_login(args.port, args.username)
            else:
                username = args.username or input('CrossPoint sync username: ').strip()
                auth_key = hashlib.md5(getpass.getpass('CrossPoint sync password: ').encode()).hexdigest()
            sources = ReadOnlySources(args.amazon_auth, username, auth_key,
                                      profile['document'], profile['crosspoint_document'])
            try:
                return run_loop(engine, sources.read, args.state, args.log,
                                count=args.count, interval=args.interval)
            finally:
                sources.close()
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({'status': 'startup_failed', 'error_type': type(error).__name__,
                          'error': 'Check local profile, credentials, state, and file permissions'}), flush=True)
        return 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
