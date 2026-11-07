import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from book_sync.mapping.chapters import ChapterMap
from book_sync.mapping.text import epub_index


class ChapterMappingTests(unittest.TestCase):
    def test_anchor_boundaries_continuations_and_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'book.epub'
            with ZipFile(path, 'w') as book:
                book.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
                book.writestr('book.opf', '<package><manifest><item id="toc" href="toc.ncx"/><item id="body" href="book.xhtml"/></manifest><spine toc="toc"><itemref idref="body"/></spine></package>')
                book.writestr('toc.ncx', '<ncx><navMap><navPoint><navLabel><text>Chapter One: Start</text></navLabel><content src="book.xhtml#one"/></navPoint><navPoint><navLabel><text>Chapter Two: End</text></navLabel><content src="book.xhtml#two"/></navPoint></navMap></ncx>')
                book.writestr('book.xhtml', '<html><body><section id="one"><p>abcdefghij</p></section><section id="two"><p>klmnopqrst</p></section></body></html>')
            index = epub_index(path)
            timing = {'chapters': [['Part 1 - Chapter 1', 0], ['Part 1 - Chapter 1 (00:00)', 5000],
                                   ['Part 1 - Chapter 2', 10000]], 'duration': 20000}
            mapping = ChapterMap(path, index.coordinates, timing)
            pointer, _ = mapping.to_text(15000)
            self.assertEqual(mapping.to_audio(pointer)[0], 15000)
            self.assertEqual(mapping.to_audio(index.coordinates[0])[0], 0)
            self.assertEqual(mapping.to_audio(index.coordinates[10])[0], 10000)
            self.assertEqual(mapping.to_text(5000)[0], index.coordinates[5])
            with self.assertRaises(ValueError):
                mapping.to_text(20000)
            timing['chapters'][-1][0] = 'Part 1 - Chapter 3'
            with self.assertRaises(ValueError):
                ChapterMap(path, index.coordinates, timing)


if __name__ == '__main__':
    unittest.main()
