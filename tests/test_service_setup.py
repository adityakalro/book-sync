import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from placekeeper.web.setup import Connections


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.connections = Connections(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_kindle_code_flow_saves_both_uses_privately(self):
        with self.assertRaises(ValueError):
            self.connections.apply('kindle', 'start', {})
        result = self.connections.apply('kindle', 'start', {'consent': True})
        url = urlsplit(result['login_url'])
        self.assertEqual(url.netloc, 'www.amazon.com')
        params = parse_qs(url.query)
        self.assertIn('openid.oa2.code_challenge', params)
        self.assertNotIn('verifier', result)
        response = Mock(status_code=200)
        response.json.return_value = {'response': {'success': {'tokens': {
            'mac_dms': {'adp_token': 'private-token', 'device_private_key': 'private-key'},
            'bearer': {'expires_in': 3600}}}}}
        with patch('placekeeper.plugins.kindle.registration.httpx.post', return_value=response) as post:
            with self.assertRaises(ValueError):
                self.connections.apply('kindle', 'finish', {'completion_url':
                    'https://example.org/ap/maplanding?openid.oa2.authorization_code=code'})
            post.assert_not_called()
            self.assertEqual(self.connections.apply('kindle', 'finish', {'completion_url':
                'https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=code'}), {'connected': True})
            request = post.call_args.kwargs['json']['auth_data']
            self.assertEqual(request['authorization_code'], 'code')
            self.assertTrue(request['code_verifier'])
        for filename in self.connections.plugins['kindle'].files:
            path = self.root / filename
            self.assertEqual(json.loads(path.read_text())['adp_token'], 'private-token')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.connections.status()['kindle']['connected'])
        self.assertFalse((self.root / 'setup/kindle.json').exists())

    def test_readest_pending_device_code_is_never_returned(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock()
        code = Mock(status_code=200)
        code.json.return_value = {'user_code': 'AAAA-BBBB', 'device_code': 'private-device-code',
            'verification_uri_complete': 'https://web.readest.com/link?code=AAAA-BBBB', 'expires_in': 300}
        token = Mock(status_code=200)
        token.json.return_value = {'access_token': 'private-reader-key'}
        client.post.side_effect = [code, token]
        with patch('placekeeper.plugins.readest.client.httpx.Client', return_value=client):
            result = self.connections.apply('readest', 'start', {'consent': True})
            self.assertNotIn('device_code', result)
            self.assertEqual(self.connections.apply('readest', 'finish', {}), {'connected': True})
        self.assertEqual(client.post.call_args.kwargs['json'], {'device_code': 'private-device-code'})
        self.assertEqual((self.root/'readest-device.json').stat().st_mode & 0o777, 0o600)

    def test_libby_device_pairing_keeps_session_private(self):
        payload = base64.urlsafe_b64encode(b'{"chip":{"id":"dummy-id"}}').decode().rstrip('=')
        token = 'header.' + payload + '.signature'
        requests = []
        def respond(request):
            requests.append((request.method, request.url.path))
            path = request.url.path
            if path == '/chip':
                return httpx.Response(200, json={'identity': token, 'chip': 'dummy-id'})
            if path == '/chip/clone/code':
                if request.url.params.get('code'):
                    return httpx.Response(200, json={'result': 'fulfilled', 'blessing': 'private-blessing'})
                return httpx.Response(200, json={'code': '12345678'})
            if path == '/chip/clone':
                return httpx.Response(200, json={})
            if path == '/chip/sync':
                return httpx.Response(200, json={'loans': []})
            return httpx.Response(404)
        real_client = httpx.Client
        def make_client(*args, **kwargs):
            return real_client(*args, transport=httpx.MockTransport(respond), **kwargs)
        with patch('placekeeper.plugins.libby.client.httpx.Client', side_effect=make_client):
            self.assertEqual(self.connections.apply('libby', 'start', {'consent': True}),
                             {'code': '12345678'})
            self.assertFalse(self.connections.status()['libby']['connected'])
            self.assertEqual(self.connections.apply('libby', 'finish', {}), {'connected': True})
        self.assertIn(('POST', '/chip/clone'), requests)
        self.assertFalse((self.root/'setup/libby.json').exists())
        self.assertEqual((self.root/'libby-device.json').stat().st_mode & 0o777, 0o600)

    def test_status_readiness_and_disconnect_are_local(self):
        for service in ('kindle', 'readest', 'libby'):
            for filename in self.connections.plugins[service].files:
                (self.root/filename).write_text('private')
        self.assertTrue(self.connections.ready())
        self.assertNotIn('private', json.dumps(self.connections.status()))
        with self.assertRaises(ValueError):
            self.connections.apply('libby', 'disconnect', {})
        self.assertTrue(self.connections.ready())
        self.connections.apply('libby', 'disconnect', {'confirm': True})
        self.assertFalse(self.connections.ready())
        self.assertTrue(self.connections.status()['kindle']['connected'])


if __name__ == '__main__':
    unittest.main()
