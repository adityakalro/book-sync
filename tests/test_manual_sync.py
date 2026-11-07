import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from xml.etree.ElementTree import fromstring

import httpx
from book_sync.legacy.ebook import apply_handoff, ManualSources, sync_registry
from book_sync.errors import MissingProgress, ReadFailure
from book_sync.sync.dry_run import Snapshot
from tests.test_sync_dry_run import LocationMap,P1,P2


class Map(LocationMap):locations=[{}]*3


class Sources:
    def __init__(self,snapshots,respond):
        self.snapshots=iter(snapshots);self.auth=None;self.headers={'x-auth-key':'test-hash'}
        self.client=httpx.Client(transport=httpx.MockTransport(respond))
    def details(self):
        value=next(self.snapshots)
        if isinstance(value,Exception):raise value
        return value,{'payload':{'guid':'test-guid'}},{'progress':value.crosspoint_xpointer}


class HandoffTests(unittest.TestCase):
    def test_crosspoint_missing_book_is_optional_for_audio_sync(self):
        with tempfile.TemporaryDirectory() as d:
            auth=Path(d)/'auth.json';auth.write_text('{}')
            with patch('audible.Authenticator.from_dict',return_value=None):
                source=ManualSources(auth,'user','test-hash','KINDLEBOOK','cp-book')
            kindle={'payload':{'type':'PDOC','key':'KINDLEBOOK',
                              'records':[{'type':'kindle.lpr','location':'150'}]}}
            try:
                missing=ReadFailure('CrossPoint read failed: HTTP 404')
                with patch.object(source,'_get',side_effect=[kindle,missing,kindle,missing]):
                    snapshot,_,_=source.details(allow_missing=True)
                    self.assertEqual(snapshot.kindle_position,150)
                    self.assertEqual(snapshot.crosspoint_xpointer,'')
                    with self.assertRaises(MissingProgress):source.details()
            finally:source.close()

    def test_registry_falls_back_when_a_matching_audiobook_has_no_position(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'onboarding').mkdir()
            path=root/'profile.json';path.write_text('{}')
            (root/'onboarding/ready-profiles.json').write_text(json.dumps({'profiles':[
                {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}))
            (root/'crosspoint-auth.json').write_text('{"username":"test","auth_key":"test"}')
            (root/'libby-device.json').write_text('{}')
            profile={'document':'TEST','crosspoint_document':'test','x4_filename':'Book.epub',
                     'files':{'epub':{'path':'/test/book.epub'}}}
            libby=Mock();libby.match.return_value={'id':'loan'}
            with patch('book_sync.plugins.libby.client.Libby',return_value=libby), \
                    patch('book_sync.legacy.ebook.load_profile',return_value=(profile,Mock(),'profile')), \
                    patch('book_sync.legacy.ebook.ManualSources'), \
                    patch('book_sync.legacy.audiobook.apply_three_way',return_value={'status':'unavailable','writes':0}), \
                    patch('book_sync.legacy.ebook.apply_handoff',return_value={'status':'in_sync','message':'Together','writes':0}) as ebook:
                result=sync_registry(root)[0]
            ebook.assert_called_once()
            self.assertEqual(result['status'],'in_sync')
            self.assertIn('Libby has no saved position',result['message'])

    def test_progress_read_recovers_from_transient_dns_error(self):
        calls=[]
        def respond(request):
            calls.append(request)
            if len(calls)==1:raise httpx.ConnectError('DNS busy')
            return httpx.Response(200,json={'result':'ok'})
        with tempfile.TemporaryDirectory() as d:
            auth=Path(d)/'auth.json';auth.write_text('{}')
            with patch('audible.Authenticator.from_dict',return_value=None):
                source=ManualSources(auth,'user','test-hash','KINDLEBOOK','cp-book',
                    httpx.Client(transport=httpx.MockTransport(respond)))
            try:
                with patch('book_sync.legacy.ebook.time.sleep'):
                    self.assertEqual(source._get('CrossPoint','https://sync.crosspointreader.com/users/auth'),{'result':'ok'})
            finally:source.close()
        self.assertEqual([r.method for r in calls],['GET','GET'])

    def run_case(self,snapshots,respond=lambda r:httpx.Response(200)):
        profile={'document':'KINDLEBOOK','crosspoint_document':'cp-book',
                 'files':{'epub':{'path':'/test/book.epub'}}}
        with tempfile.TemporaryDirectory() as d:
            sources=Sources(snapshots,respond)
            try:
                with patch('book_sync.legacy.ebook.epub_position_details',return_value={'percentage':0.5}):
                    return apply_handoff(profile,Map(),'profile',sources,Path(d))
            finally:sources.client.close()

    def test_kindlei_to_crosspoint_writes_one_record_then_no_echo(self):
        requests=[]
        def respond(r):
            requests.append(r)
            self.assertEqual(r.method,'PUT');self.assertEqual(r.url.host,'sync.crosspointreader.com')
            self.assertEqual(json.loads(r.content)['progress'],P2)
            return httpx.Response(200)
        result=self.run_case([Snapshot(200,P1),Snapshot(200,P1),Snapshot(200,P2)],respond)
        self.assertEqual(result['status'],'synced');self.assertEqual(len(requests),1)

    def test_crosspoint_to_kindle_keeps_other_annotations_out_of_payload(self):
        requests=[]
        def respond(r):
            requests.append(r);self.assertEqual(r.method,'POST')
            self.assertEqual(r.url.host,'cde-ta-g7g.amazon.com')
            root=fromstring(r.content)
            book=root.find('book');self.assertEqual(book.attrib['key'],'KINDLEBOOK')
            self.assertEqual(book.attrib['guid'],'test-guid')
            self.assertEqual([e.tag for e in book],['last_read'])
            self.assertEqual(book.find('last_read').attrib['begin'],'299')
            return httpx.Response(200)
        result=self.run_case([Snapshot(10,P2),Snapshot(10,P2),Snapshot(299,P2)],respond)
        self.assertEqual(result['status'],'synced');self.assertEqual(len(requests),1)

    def test_in_sync_never_writes(self):
        result=self.run_case([Snapshot(200,P2)],lambda r:self.fail('Unexpected write'))
        self.assertEqual(result['writes'],0)

    def test_position_changed_before_write_is_skipped(self):
        result=self.run_case([Snapshot(10,P2),Snapshot(200,P2)],lambda r:self.fail('Unexpected write'))
        self.assertEqual(result['status'],'changed');self.assertEqual(result['writes'],0)

    def test_unknown_network_outcome_is_not_retried(self):
        requests=[]
        def respond(r):
            requests.append(r);raise httpx.ReadTimeout('unknown')
        with self.assertRaisesRegex(ReadFailure,'outcome is unknown'):
            self.run_case([Snapshot(200,P1),Snapshot(200,P1)],respond)
        self.assertEqual(len(requests),1)

    def test_readback_failure_keeps_accepted_write_visible(self):
        result=self.run_case([Snapshot(200,P1),Snapshot(200,P1),ReadFailure('offline')])
        self.assertEqual(result['status'],'unconfirmed');self.assertEqual(result['writes'],1)


if __name__=='__main__':unittest.main()
