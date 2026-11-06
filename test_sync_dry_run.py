import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sync_dry_run import (DryRunEngine, ReadFailure, ReadOnlySources, Snapshot,
                          crosspoint_progress, kindle_position, load_profile,
                          private_json, run_loop, state_lock)


P1 = '/body/DocFragment[1]/body/p[1]/text()[1].0'
P2 = '/body/DocFragment[1]/body/p[2]/text()[1].0'
P3 = '/body/DocFragment[1]/body/p[3]/text()[1].0'


class LocationMap:
    """Small coordinate domains for exercising decisions independently of text."""
    def from_xpointer(self, xpath):
        location = [P1, P2, P3].index(xpath)+1
        return {'sidecar_range': [(location-1)*150, location*150-1],
                'suggested_forward_position': location*150-1}

    def from_sidecar(self, position):
        if not 0 <= position < 450:
            raise ValueError('Unmapped position')
        return {'xpointer': [P1, P2, P3][position//150]}


class DecisionTests(unittest.TestCase):
    def engine(self, state=None):
        return DryRunEngine(LocationMap(), 'book-a', state)

    def test_same_bucket_does_not_round_trip_to_a_new_proposal(self):
        engine = self.engine()
        for kindle in (150, 157, 299):
            self.assertEqual(engine.observe(Snapshot(kindle, P2))['status'], 'in_sync')

    def test_baseline_mismatch_proposes_forward_only(self):
        event = self.engine().observe(Snapshot(10, P2))
        self.assertTrue(event['baseline'])
        self.assertEqual(event['proposal']['direction'], 'crosspoint_to_kindle')
        self.assertEqual(event['proposal']['target_sidecar_position'], 299)
        event = self.engine().observe(Snapshot(320, P2))
        self.assertEqual(event['proposal']['direction'], 'kindle_to_crosspoint')
        self.assertEqual(event['proposal']['target_xpointer'], P3)

    def test_duplicate_proposal_suppressed_across_restart(self):
        engine = self.engine()
        first = engine.observe(Snapshot(10, P2, 'X4', 1))
        engine = self.engine(json.loads(json.dumps(engine.state)))
        second = engine.observe(Snapshot(10, P2, 'X4', 999))
        self.assertEqual(second['status'], 'proposal_unchanged')
        self.assertEqual(second['changed'], [])
        self.assertEqual(first['proposal']['id'], second['proposal']['id'])

    def test_both_handoff_directions_resolve_without_echo(self):
        for before, after in ((Snapshot(10, P2), Snapshot(299, P2)),
                              (Snapshot(320, P2), Snapshot(320, P3))):
            with self.subTest(before=before):
                engine = self.engine()
                engine.observe(before)
                event = engine.observe(after)
                self.assertEqual(event['status'], 'destination_caught_up')
                self.assertIsNone(engine.state['pending'])
                self.assertNotIn('proposal', event)
                self.assertEqual(engine.observe(after)['status'], 'in_sync')

    def test_new_reading_replaces_pending_target(self):
        engine = self.engine()
        engine.observe(Snapshot(10, P2))
        event = engine.observe(Snapshot(10, P3))
        self.assertEqual(event['status'], 'would_sync')
        self.assertEqual(event['changed'], ['crosspoint'])
        self.assertEqual(event['proposal']['target_sidecar_position'], 449)

    def test_both_services_change_choose_farther_not_newer_timestamp(self):
        engine = self.engine()
        engine.observe(Snapshot(10, P1))
        event = engine.observe(Snapshot(200, P3, crosspoint_timestamp=1))
        self.assertEqual(event['changed'], ['kindle', 'crosspoint'])
        self.assertEqual(event['proposal']['direction'], 'crosspoint_to_kindle')

    def test_failed_mapping_keeps_last_good_baseline(self):
        engine = self.engine()
        engine.observe(Snapshot(10, P1))
        before = json.loads(json.dumps(engine.state))
        with self.assertRaises(ValueError):
            engine.observe(Snapshot(99999, P2))
        self.assertEqual(engine.state, before)

    def test_different_book_state_is_rejected(self):
        with self.assertRaises(ValueError):
            DryRunEngine(LocationMap(), 'book-b', self.engine().state)


class SourceTests(unittest.TestCase):
    def test_wrong_book_and_missing_progress_fail_closed(self):
        with self.assertRaises(ReadFailure):
            kindle_position({'payload': {'key': 'wrong', 'type': 'PDOC'}}, 'book')
        with self.assertRaises(ReadFailure):
            kindle_position({'payload': {'key': 'book', 'type': 'PDOC', 'records': []}}, 'book')
        with self.assertRaises(ReadFailure):
            crosspoint_progress({'document': 'wrong', 'progress': P1}, 'book')
        with self.assertRaises(ReadFailure):
            crosspoint_progress({'document': 'book', 'progress': '23%'}, 'book')
        for payload in (None, [], {'key': 'book', 'type': 'PDOC', 'records': [None]}):
            with self.assertRaises(ReadFailure):
                kindle_position({'payload': payload}, 'book')

    def test_http_transport_only_gets_expected_services_and_documents(self):
        import httpx
        requests = []
        def respond(request):
            requests.append(request)
            self.assertEqual(request.method, 'GET')
            if request.url.host == 'cde-ta-g7g.amazon.com':
                self.assertEqual(request.url.params['key'], 'KINDLEBOOK')
                self.assertNotIn('x-auth-key', request.headers)
                return httpx.Response(200, json={'payload': {'key': 'KINDLEBOOK', 'type': 'PDOC',
                                      'records': [{'type': 'kindle.lpr', 'location': '320'}]}})
            self.assertEqual(request.url.host, 'sync.crosspointreader.com')
            self.assertEqual(request.url.path, '/syncs/progress/cp-book')
            self.assertEqual(request.headers['x-auth-key'], 'secret-hash')
            return httpx.Response(200, json={'document': 'cp-book', 'progress': P2, 'timestamp': 1})
        # Inject a no-op auth object; real credential material never enters tests.
        with tempfile.TemporaryDirectory() as directory, patch('audible.Authenticator.from_dict', return_value=None):
            auth = Path(directory)/'auth.json'
            auth.write_text('{}')
            sources = ReadOnlySources(auth, 'user', 'secret-hash', 'KINDLEBOOK', 'cp-book',
                                      httpx.Client(transport=httpx.MockTransport(respond)))
            try:
                snapshot = sources.read()
            finally:
                sources.close()
        self.assertEqual(snapshot.kindle_position, 320)
        self.assertEqual(snapshot.crosspoint_xpointer, P2)
        self.assertEqual(len(requests), 2)

    def test_http_errors_do_not_leak_response_or_credentials(self):
        import httpx
        with tempfile.TemporaryDirectory() as directory, patch('audible.Authenticator.from_dict', return_value=None):
            auth = Path(directory)/'auth.json'
            auth.write_text('{}')
            sources = ReadOnlySources(auth, 'user', 'secret-hash', 'book', 'cp',
                httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text='sensitive body'))))
            try:
                with self.assertRaisesRegex(ReadFailure, '^Kindle read failed: HTTP 401$'):
                    sources.read()
            finally:
                sources.close()


class PersistenceTests(unittest.TestCase):
    def test_partial_read_error_does_not_replace_checkpoint(self):
        engine = DryRunEngine(LocationMap(), 'book-a')
        with tempfile.TemporaryDirectory() as directory:
            state, log = Path(directory)/'state.json', Path(directory)/'events.jsonl'
            snapshots = iter([Snapshot(10, P1), ReadFailure('CrossPoint connection failed'), Snapshot(10, P2)])
            def read():
                result = next(snapshots)
                if isinstance(result, Exception):
                    raise result
                return result
            with patch('sync_dry_run.time.sleep'), contextlib.redirect_stdout(io.StringIO()):
                status = run_loop(engine, read, state, log, count=3, interval=1)
            events = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(status, 1)
            self.assertEqual(events[1]['status'], 'observation_failed')
            self.assertEqual(events[2]['changed'], ['crosspoint'])
            self.assertEqual(json.loads(state.read_text()), engine.state)
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_second_loop_cannot_use_same_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            with state_lock(path):
                with self.assertRaisesRegex(ValueError, 'Another loop'):
                    with state_lock(path):
                        self.fail('lock should prevent this')

    def test_modified_book_is_rejected_before_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root/'.private'
            private.mkdir()
            (private/'changed.epub').write_text('a new edition')
            profile = {'document': 'KINDLEBOOK', 'crosspoint_document': 'a'*32,
                       'files': {'epub': {'path': '.private/changed.epub', 'sha256': 'wrong'}}}
            path = private/'profile.json'
            path.write_text(json.dumps(profile))
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                load_profile(path)


PRIVATE_PROFILE=os.environ.get('BOOK_SYNC_TEST_PROFILE','')


@unittest.skipUnless(PRIVATE_PROFILE and Path(PRIVATE_PROFILE).is_file(),
                     'Optional private validation profile is not configured')
class PrivateBookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile, cls.mapper, cls.profile_id = load_profile(PRIVATE_PROFILE)

    def test_confirmed_book_handoffs_in_both_directions(self):
        anchor = self.profile['highlight_check']['predicted']['xpointer']
        target = self.profile['physical_handoff_check']['predicted']['xpointer']
        before = self.profile['highlight_check']['observed_sidecar_start']
        after = self.profile['physical_handoff_check']['observed_successful_sidecar_write']
        engine = DryRunEngine(self.mapper, self.profile_id)
        self.assertEqual(engine.observe(Snapshot(before, anchor))['status'], 'in_sync')
        forward = engine.observe(Snapshot(before, target))
        self.assertEqual(forward['proposal']['target_sidecar_position'], after)
        self.assertEqual(engine.observe(Snapshot(before, target))['status'], 'proposal_unchanged')
        self.assertEqual(engine.observe(Snapshot(after, target))['status'], 'destination_caught_up')
        reverse = engine.observe(Snapshot(after, anchor))
        self.assertEqual(reverse['proposal']['target_xpointer'], target)
        self.assertEqual(engine.observe(Snapshot(after, anchor))['status'], 'proposal_unchanged')
        self.assertEqual(engine.observe(Snapshot(after, target))['status'], 'destination_caught_up')


if __name__ == '__main__':
    unittest.main()
