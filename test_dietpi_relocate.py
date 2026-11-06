import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from dietpi_relocate import relocate


class RelocationTests(unittest.TestCase):
    def test_moves_paths_and_rehashes_registry_without_changing_book_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            old='/old-host/book-sync/.private'
            book=root/'onboarding/books/BOOK'
            book.mkdir(parents=True)
            epub=root/'onboarding/epubs/book.epub';epub.parent.mkdir();epub.write_bytes(b'book-data')
            expected=hashlib.sha256(epub.read_bytes()).hexdigest()
            profile={'files':{'epub':{'path':old+'/onboarding/epubs/book.epub','sha256':expected}}}
            p=book/'profile.json';p.write_text(json.dumps(profile))
            state={'amazon':{'cursor':'unchanged'},'jobs':{'BOOK':{'status':'ready',
                'profile':old+'/onboarding/books/BOOK/profile.json',
                'files':profile['files'],'profile_sha256':'old-profile-hash'}}}
            (root/'onboarding/state.json').write_text(json.dumps(state))
            relocate(root,old)
            migrated=json.loads(p.read_text())
            self.assertEqual(migrated['files']['epub']['path'],str(epub))
            self.assertEqual(hashlib.sha256(epub.read_bytes()).hexdigest(),expected)
            registry=json.loads((root/'onboarding/ready-profiles.json').read_text())
            self.assertEqual(registry['profiles'][0]['sha256'],hashlib.sha256(p.read_bytes()).hexdigest())
            self.assertEqual(registry['profiles'][0]['path'],str(p))
            self.assertEqual(json.loads((root/'onboarding/state.json').read_text())['amazon']['cursor'],'unchanged')


if __name__=='__main__':unittest.main()
