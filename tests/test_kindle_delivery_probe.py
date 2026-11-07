import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from placekeeper.plugins.kindle.delivery import discover_documents, download_kfx


class DeliveryTests(unittest.TestCase):
    def manifest(self, url='https://kindle-digital-delivery.amazon.com/content?token=test'):
        return {'content': {'id': 'DOC'}, 'resources': [{'type': 'KINDLE_MAIN_BASE',
                'optimalEndpoint': {'directUrl': url}, 'size': 8}]}

    def test_foreign_host_is_not_requested(self):
        with tempfile.TemporaryDirectory() as directory, httpx.Client() as client:
            with patch.object(client, 'stream') as stream:
                with self.assertRaisesRegex(ValueError, 'supported Amazon'):
                    download_kfx('DOC', self.manifest('https://example.com/file'), directory, client)
                stream.assert_not_called()

    def test_wrong_document_is_not_requested(self):
        with tempfile.TemporaryDirectory() as directory, httpx.Client() as client:
            with patch.object(client, 'stream') as stream:
                with self.assertRaisesRegex(ValueError, 'requested document'):
                    download_kfx('OTHER', self.manifest(), directory, client)
                stream.assert_not_called()

    def test_error_page_or_truncated_file_is_not_committed(self):
        for body in (b'<html>error</html>', b'CONT'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))) as client:
                    with self.assertRaises(ValueError):
                        download_kfx('DOC', self.manifest(), directory, client)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_content_download_uses_no_account_credentials_and_is_private(self):
        def respond(request):
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.host, 'kindle-digital-delivery.amazon.com')
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('x-adp-token', request.headers)
            return httpx.Response(200, content=b'CONTtest')
        with tempfile.TemporaryDirectory() as directory:
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                report = download_kfx('DOC', self.manifest(), directory, client)
            self.assertTrue(report['downloaded'])
            self.assertEqual((Path(directory)/'DOC.azw8').stat().st_mode & 0o777, 0o600)
            self.assertNotIn('token=test', json.dumps(report))

    def test_discovery_keeps_only_personal_document_metadata_and_pagination_flag(self):
        xml = ('<response><has_more>true</has_more><sync_time>123</sync_time><add_update_list>'
               '<meta_data><ASIN>DOC</ASIN><title>My document</title><cde_contenttype>PDOC</cde_contenttype></meta_data>'
               '<meta_data><ASIN>BOOK</ASIN><title>Unrelated purchased book</title><cde_contenttype>EBOK</cde_contenttype></meta_data>'
               '</add_update_list></response>')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root/'auth.json';auth.write_text('{}')
            client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=xml)))
            with patch('placekeeper.plugins.kindle.delivery.Authenticator.from_dict', return_value=None), \
                    patch('placekeeper.plugins.kindle.delivery.httpx.Client', return_value=client):
                report = discover_documents(auth, root/'output')
            saved = (root/'output/personal-documents.json').read_text()
            self.assertEqual(report['personal_documents'], 1)
            self.assertEqual(report['has_more'], 'true')
            self.assertNotIn('Unrelated purchased book', saved)


if __name__ == '__main__':
    unittest.main()
