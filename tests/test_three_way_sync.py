import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from placekeeper.errors import MissingProgress, ReadFailure
from placekeeper.legacy.audiobook import apply_three_way, select_source, read_records
from placekeeper.plugins.kindle.progress import parse_progress_time


class TimestampTests(unittest.TestCase):
    def test_amazon_relative_dates_use_server_utc_day(self):
        xml = '<book><last_read pos="150" method="FRL">Furthest at 11:59 PM GMT yesterday.</last_read></book>'
        result = parse_progress_time(xml, 'Sat, 07 Nov 2026 04:45:02 GMT', 150)
        self.assertEqual(result.isoformat(), '2026-11-06T23:59:00+00:00')
        absolute = xml.replace('11:59 PM GMT yesterday', '10:42 PM GMT on November 05, 2026')
        self.assertEqual(parse_progress_time(absolute, 'Sat, 07 Nov 2026 04:45:02 GMT', 150).isoformat(),
                         '2026-11-05T22:42:00+00:00')
        with self.assertRaises(ValueError):
            parse_progress_time(xml, 'Sat, 07 Nov 2026 04:45:02 GMT', 300)

    def test_own_kindle_write_can_be_verified_without_an_amazon_time(self):
        sources = Mock()
        snapshot = Mock(kindle_position=449, crosspoint_xpointer='target',
                        crosspoint_timestamp=3000, crosspoint_device='X4')
        sources.details.return_value = (snapshot, {}, {})
        libby = Mock()
        libby.state = {'chipId': 'placekeeper'}
        libby.position.return_value = {'timestamp': 4000, 'deviceId': 'phone'}
        state = {'receipts': {}, 'pending': {'device': 'Kindle', 'target': 449,
                 'before': {'position': 150, 'stamp': 1000}, 'stamp': 5000}}
        with patch('placekeeper.legacy.audiobook.fetch_progress_time') as fetch, patch('placekeeper.legacy.audiobook.elapsed', return_value=120000):
            records, _ = read_records(sources, {}, libby, {}, {}, state)
            self.assertTrue(records['Kindle']['own'])
            self.assertEqual(records['Kindle']['stamp'], 5000)
            fetch.assert_not_called()
            snapshot.kindle_position = 600
            fetch.return_value.timestamp.return_value = 6000
            records, _ = read_records(sources, {}, libby, {}, {}, state)
            self.assertFalse(records['Kindle']['own'])
            fetch.assert_called_once()

    def test_latest_source_and_receipts(self):
        records = {name: {'position': i, 'stamp': stamp}
                   for i, (name, stamp) in enumerate([('Kindle', 1000), ('X4', 2000), ('Libby', 3000)])}
        for source in records:
            values = copy.deepcopy(records)
            values[source]['stamp'] = 4000
            self.assertEqual(select_source(values, {}), source)
        self.assertEqual(select_source(records, {'Libby': records['Libby']}), 'X4')
        records['X4']['stamp'] = 2990
        with self.assertRaises(ReadFailure):
            select_source(records, {})


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.records = {
            'Kindle': {'position': 150, 'stamp': 1000},
            'X4': {'position': 'old', 'stamp': 2000, 'own': False},
            'Libby': {'position': 120000, 'stamp': 3000, 'own': False},
        }
        self.profile = {'files': {'epub': {'path': '/test/book.epub'}}, 'document': 'test'}
        self.mapper = Mock()
        self.mapper.from_xpointer.return_value = {'suggested_forward_position': 449}
        self.mapper.from_sidecar.return_value = {'xpointer': 'target'}
        self.chapters = Mock()
        self.chapters.to_text.return_value = ('target', 'Chapter One')
        self.chapters.to_audio.side_effect = lambda pointer: (0 if pointer == 'old' else 120000, 'Chapter One')
        self.libby = Mock()
        self.libby.open_player.return_value = (Mock(), 'https://player/', {})
        self.patches = [
            patch('placekeeper.legacy.audiobook.ChapterMap', return_value=self.chapters),
            patch('placekeeper.legacy.audiobook.read_records', side_effect=lambda *args: (copy.deepcopy(self.records), {})),
            patch('placekeeper.legacy.audiobook.write_target', side_effect=self.write),
        ]
        self.writers = [p.start() for p in self.patches]

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def write(self, device, target, *args):
        self.records[device]['position'] = target
        self.records[device]['stamp'] = 5000
        if device != 'Kindle':
            self.records[device]['own'] = True

    def run_sync(self, **kwargs):
        return apply_three_way(self.profile, self.mapper, Mock(), self.directory,
                               self.libby, {}, **kwargs)

    def test_libby_updates_ebooks_then_second_run_is_idle(self):
        result = self.run_sync()
        self.assertEqual(result['writes'], 2)
        self.assertEqual(result['source'], 'Libby')
        self.assertEqual(result['status'], 'synced')
        self.assertEqual(self.run_sync()['writes'], 0)
        self.assertEqual(self.writers[-1].call_count, 2)

    def test_ebook_sources_write_libby(self):
        for source in ('Kindle', 'X4'):
            with self.subTest(source=source):
                self.records['Kindle']['stamp'] = 1000
                self.records['X4']['stamp'] = 2000
                self.records['Libby']['position'] = 0
                self.records['X4']['position'] = 'target'
                self.records[source]['stamp'] = 4000
                self.records[source]['own'] = False
                result = self.run_sync(dry_run=True)
                self.assertEqual(result['source'], source)
                self.assertIn('Libby would update', result['message'])
                self.records['Libby']['position'] = 0
        self.assertEqual(self.writers[-1].call_count, 0)

    def test_source_change_after_write_stops_remaining_destination(self):
        original = self.write
        def change(device, target, *args):
            original(device, target, *args)
            self.records['Libby']['position'] += 10000
        self.writers[-1].side_effect = change
        result = self.run_sync()
        self.assertEqual(result['status'], 'changed')
        self.assertEqual(self.writers[-1].call_count, 1)

    def test_failed_write_remains_pending_and_is_not_blindly_retried(self):
        self.writers[-1].side_effect = ReadFailure('Connection lost')
        first = self.run_sync()
        self.assertEqual(first['status'], 'unconfirmed')
        second = self.run_sync()
        self.assertEqual(second['status'], 'error')
        self.assertEqual(self.writers[-1].call_count, 1)

    def test_kindles_furthest_marker_is_kept_when_ahead(self):
        self.records['Kindle']['position'] = 900
        result = self.run_sync()
        self.assertEqual(result['writes'], 1)
        self.assertIn('Kindle is ahead', result['message'])

    def test_kindle_and_libby_sync_without_crosspoint(self):
        del self.records['X4']
        result = self.run_sync()
        self.assertEqual(result['status'], 'synced')
        self.assertEqual(result['writes'], 1)
        self.assertEqual(self.writers[-1].call_args.args[0], 'Kindle')

    def test_unstarted_kindle_book_leaves_libby_and_x4_available(self):
        del self.records['Kindle']
        result = self.run_sync()
        self.assertEqual(result['status'], 'synced')
        self.assertEqual(result['writes'], 1)
        self.assertEqual(self.writers[-1].call_args.args[0], 'X4')

    def test_unstarted_audiobook_can_use_eplacekeeper(self):
        self.libby.position.side_effect = MissingProgress('No listening position')
        self.assertEqual(self.run_sync()['status'], 'unavailable')
        self.libby.open_player.assert_not_called()
        self.writers[-1].assert_not_called()

    def test_only_one_reader_has_a_position(self):
        del self.records['Kindle']
        del self.records['X4']
        self.assertEqual(self.run_sync()['status'], 'skipped')
        self.writers[-1].assert_not_called()

    def test_libby_connection_failure_does_not_fall_back_to_ebook_writes(self):
        self.libby.position.side_effect = ReadFailure('Could not reach Libby')
        self.assertEqual(self.run_sync()['status'], 'error')
        self.writers[-1].assert_not_called()


if __name__ == '__main__':
    unittest.main()
