import json
from pathlib import Path
import tempfile
import unittest

import httpx
from book_sync.plugins.readest.client import ReadestSource


class ReadestTests(unittest.TestCase):
    def source(self,directory,handler):
        auth=Path(directory)/'auth.json';auth.write_text(json.dumps({'access_token':'private-reader-key'}))
        return ReadestSource(auth,httpx.Client(transport=httpx.MockTransport(handler)))

    def test_paginated_catalog_keeps_extra_row_for_next_page(self):
        requests=[]
        items=[{'id':f'{i:028x}','title':f'Book {i}'} for i in range(51)]
        def respond(request):
            requests.append(request)
            self.assertEqual(request.method,'GET')
            self.assertEqual(request.headers['authorization'],'Bearer private-reader-key')
            page=int(request.url.params['page'])
            return httpx.Response(200,json={'items':items if page==1 else items[50:]})
        with tempfile.TemporaryDirectory() as d:
            source=self.source(d,respond)
            try:books=source.books()
            finally:source.close()
        self.assertEqual(len(books),51)
        self.assertEqual(len(requests),2)

    def test_server_repeating_a_full_page_is_rejected(self):
        items=[{'id':f'{i:028x}','title':str(i)} for i in range(51)]
        with tempfile.TemporaryDirectory() as d:
            source=self.source(d,lambda r:httpx.Response(200,json={'items':items}))
            try:
                with self.assertRaisesRegex(ValueError,'repeated'):source.books()
            finally:source.close()

    def test_auth_error_is_not_an_empty_library(self):
        with tempfile.TemporaryDirectory() as d:
            source=self.source(d,lambda r:httpx.Response(401,text='secret details'))
            try:
                with self.assertRaisesRegex(ValueError,'HTTP 401'):source.books()
            finally:source.close()

    def test_cloud_file_request_never_carries_reader_key(self):
        requests=[]
        epub=Path(Path(__file__).parent/'fixtures/Book Sync Progress Test.epub').read_bytes()
        def respond(request):
            requests.append(request)
            if request.url.host=='web.readest.com':
                self.assertIn('authorization',request.headers)
                return httpx.Response(200,json={'downloadUrl':'https://storage.example.org/book.epub?signature=test'})
            self.assertEqual(request.url.host,'storage.example.org')
            self.assertNotIn('authorization',request.headers)
            return httpx.Response(200,content=epub)
        with tempfile.TemporaryDirectory() as d:
            source=self.source(d,respond)
            try:p=source.download({'id':'a'*28},Path(d)/'epubs')
            finally:source.close()
            self.assertEqual(p.read_bytes(),epub)
            self.assertEqual(p.stat().st_mode&0o777,0o600)
        self.assertEqual(len(requests),2)


if __name__=='__main__':unittest.main()
