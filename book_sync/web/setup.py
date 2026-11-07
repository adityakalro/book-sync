"""Owner-controlled service connections for a single Book Sync installation."""
import json
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

import httpx

import book_sync.plugins.kindle.registration as kindle
from book_sync.plugins.libby.client import Libby
from book_sync.plugins.readest.client import pairing_start, pairing_finish
from book_sync.errors import ReadFailure
from book_sync.storage import private_json

FILES = {
    'kindle': ('amazon-device.json', 'amazon-kindle-download-device.json'),
    'readest': ('readest-device.json',),
    'crosspoint': ('crosspoint-auth.json',),
    'libby': ('libby-device.json',),
}


class Connections:
    def __init__(self, root):
        self.root = Path(root)
        self.pending = self.root / 'setup'
        self.pending.mkdir(mode=0o700, parents=True, exist_ok=True)

    def status(self):
        return {name: {'connected': all((self.root / file).exists() for file in files),
                       'can_disconnect': any((self.root / file).exists() for file in files)
                       or (self.pending / (name + '.json')).exists()}
                for name, files in FILES.items()}

    def ready(self):
        services = self.status()
        return (services['kindle']['connected'] and services['readest']['connected']
                and (services['crosspoint']['connected'] or services['libby']['connected']))

    def apply(self, service, action, data):
        if not isinstance(service, str) or service not in FILES or action not in ('start', 'finish', 'disconnect'):
            raise ValueError('Unknown setup action.')
        pending = self.pending / (service + '.json')
        if action == 'disconnect':
            if data.get('confirm') is not True:
                raise ValueError('Confirm disconnecting this service.')
            for file in FILES[service]:
                (self.root / file).unlink(missing_ok=True)
            pending.unlink(missing_ok=True)
            if service in ('kindle', 'readest'):
                for file in ('state.json', 'ready-profiles.json', 'last-run.json'):
                    (self.root / 'onboarding' / file).unlink(missing_ok=True)
            return {'connected': False}
        if self.status()[service]['connected']:
            raise ValueError('This service is already connected. Disconnect it before connecting another account.')
        if action == 'start':
            if data.get('consent') is not True:
                raise ValueError('Confirm linking this account and saving its access key on this installation.')
            if service == 'readest':
                return pairing_start(self.root / FILES[service][0], pending)
            if service == 'libby':
                client = Libby(pending)
                try:
                    return client.pairing_start()
                finally:
                    client.close()
            if service == 'kindle':
                if any((self.root / file).exists() for file in FILES['kindle']):
                    raise ValueError('Kindle setup is incomplete. Disconnect it before reconnecting.')
                if pending.exists() and json.loads(pending.read_text()).get('phase') == 'registering':
                    raise ValueError('The earlier registration outcome is uncertain. Check Amazon’s registered devices before starting over.')
                prepared = kindle.plan()
                url, client_id, verifier = kindle.login_details(prepared)
                private_json(pending, {'plan': prepared, 'client_id': client_id, 'verifier': verifier,
                                       'expires': time.time()+600, 'phase': 'sign_in'})
                return {'login_url': url, 'device_name': prepared['device_name']}
            raise ValueError('Use the CrossPoint username and password form.')
        if not pending.exists():
            raise ValueError('Start a new connection first.')
        if service == 'readest':
            pairing_finish(self.root / FILES[service][0], pending)
        elif service == 'libby':
            client = Libby(pending)
            try:
                if client.state.get('expires', 0) < time.time():
                    raise ValueError('This Libby setup code expired. Get a new code.')
                client.pairing_finish(self.root / FILES[service][0])
            finally:
                client.close()
        elif service == 'kindle':
            saved = json.loads(pending.read_text())
            if saved['expires'] < time.time() or saved['phase'] != 'sign_in':
                raise ValueError('This sign-in expired or was already submitted. Start a new connection.')
            callback = urlsplit(str(data.get('completion_url', '')).strip())
            if callback.scheme != 'https' or callback.netloc != 'www.amazon.com' or callback.path != '/ap/maplanding':
                raise ValueError('Paste the full Amazon completion-page address.')
            fields = parse_qs(callback.query)
            codes = fields.get('openid.oa2.authorization_code', [])
            if len(codes) != 1 or not codes[0]:
                raise ValueError('The completion address has no authorization code. Finish Amazon sign-in first.')
            authorization = {'client_id': saved['client_id'], 'authorization_code': codes[0],
                             'code_verifier': saved['verifier'], 'code_algorithm': 'SHA-256',
                             'client_domain': 'DeviceLegacy'}
            saved['phase'] = 'registering'
            private_json(pending, saved)
            destination = self.root / 'amazon-kindle-download-device.json'
            kindle.register(saved['plan'], authorization=authorization, destination=destination, data_root=self.root)
            credentials = json.loads(destination.read_text())
            private_json(self.root / 'amazon-device.json', credentials)
            pending.unlink(missing_ok=True)
        else:
            raise ValueError('Unknown setup action.')
        return {'connected': True}
