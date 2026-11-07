# Book Sync

Book Sync helps you pick up where you left off across Send to Kindle documents, an X4 running CrossPoint, and Libby audiobooks. It runs on your own Linux host, including a Raspberry Pi Zero 2 W running DietPi. Open the private web page and click **Sync now** whenever you want to transfer your place.

## Before you start

- A Linux host with Git and Python 3.13, including its `venv` module. On Debian 13 or a DietPi install based on it, install these with `sudo apt install git python3 python3-venv`. Run the command without `sudo` if you are already root.
- An Amazon account with a **Send to Kindle** personal document and the matching EPUB in **Readest Cloud**. Use the same ebook edition in both places.
- At least one sync destination. Use the sync account configured on your **CrossPoint X4**, an active **Libby audiobook loan**, or both.

Each installation uses one household’s accounts. The included Kindle catalog uses Readest to match editions. Additional plugins can provide their own verified catalogs. Kindle positions are approximate to a location bucket, and Libby positions are approximate within matched chapters. Amazon purchases and other document formats have not been validated.

## 1. Install Book Sync

Run these commands on the Linux host as the user who will run Book Sync:

```sh
git clone https://github.com/adityakalro/book-sync.git
cd book-sync
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-onboarding.txt
install -d -m 700 .private .private/vendor
```

Install the external KFX parser into the private directory. This is the pinned source revision used by the project. Keep the checkout intact so the parser’s license and bundled modules stay with it.

```sh
git init .private/vendor/kindle.koplugin
git -C .private/vendor/kindle.koplugin fetch --depth 1 \
  https://github.com/kaikozlov/kindle.koplugin.git 501488fd99ace3ea143a8693b9fa7c6421101d51
git -C .private/vendor/kindle.koplugin checkout --detach FETCH_HEAD
ln -s kindle.koplugin/python .private/vendor/python
```

## 2. Start the private web page

```sh
cp deploy/web-config.example.json .private/web-config.json
.venv/bin/python -m book_sync.web.app
```

Open [Setup](http://127.0.0.1:8780/setup) in a browser on the host. If the host is a headless Pi, leave Book Sync running and open a second terminal **on your computer**. Forward the port over SSH, substituting your own SSH username and Pi address:

```sh
ssh -N -L 127.0.0.1:8780:127.0.0.1:8780 YOUR_USER@YOUR_PI_HOST
```

Keep the tunnel open while using the same [local Setup address](http://127.0.0.1:8780/setup). For startup at boot or access over a trusted LAN or WireGuard, see the [Linux service guide](docs/deployment.md).

## 3. Connect your accounts

| Service | What to do in **Setup** |
| --- | --- |
| Kindle | Approve device registration and start Amazon sign-in. Sign in on Amazon, then paste the full address of the completion page into Book Sync and select **Complete connection**. |
| Readest | Select **Get Readest code**, open the approval link, and approve the reader in Readest. Return to Book Sync and select **Check approval**. |
| CrossPoint | Enter the sync username and password configured on the X4. Approve saving the sign-in key and select **Connect CrossPoint**. |
| Libby | Select **Get Libby code**. In Libby, use **Menu → Copy To Another Device** and enter the code. Return to Book Sync and select **Check connection**. |

Once Setup says **Ready to sync**, return to the **Sync** page. The credentials are saved with owner-only permissions under `.private/`. **Disconnect** removes a connection from this installation. Manage remote device or reader grants in the provider’s account settings.

## 4. Sync your first book

1. Send the EPUB to Kindle with **Add to your library** enabled. Upload the same edition to **Readest Cloud** so Book Sync can download it.
2. Open the personal document on Kindle, turn a page, and sync Kindle. If you use an X4, download the EPUB from Readest, keep its `Title.epub` filename, use filename-based document matching, and run **Sync Progress** in the book. If you use Libby, open the matching audiobook loan, listen briefly, and sync your shelf.
3. In Book Sync, click **Sync now**. The first run discovers the book and builds its map before transferring progress. A long book can take a minute or more on a small Pi. The result appears on the page.
4. On the destination, use **Sync Progress** on X4, **Sync to Furthest Page Read** on Kindle, or **Shelf → Actions → Synchronize Shelf** in Libby. Open the book or audiobook to pick up the saved position.

For future handoffs, sync the device you were using before clicking **Sync now**. Books with matching Libby loans use the newest saved progress timestamp. Books with Kindle and X4 progress alone move forward. Kindle keeps its furthest-read marker. Book Sync runs when you click the button.

## Using only some services

For the built-in catalog, Kindle and Readest provide the book maps. Connect CrossPoint, Libby, or both. A book can sync between any two connected reading services with saved positions. A missing X4 position or an unread Kindle copy can be left out of an audio handoff. An audiobook loan without saved listening progress leaves Kindle and X4 on their normal ebook sync path. When fewer than two positions are available, the book is skipped.

If a connected service returns a connection or authentication error, Book Sync reports it for that book. It can still process the other books in the job.

## If a book does not sync

- **Waiting for an EPUB in Readest:** Confirm the matching edition is in Readest Cloud and the Kindle copy is in your Send to Kindle library.
- **Missing X4 progress:** Check the filename and matching mode, then open the book on the X4 and select **Sync Progress**.
- **Libby title or chapters could not be matched:** Check that the active loan is the same book and author. Audio sync needs a compatible chapter sequence.
- **Reader timestamps are too close:** Sync the device you want to use as the source, then retry. Kindle’s reported time has minute precision.
- **Connection failed:** Check **Setup**, your network, and your device’s cloud sync. The providers’ unofficial endpoints may change.

## Onboarding and maps

```sh
.venv/bin/python -m book_sync.sync.onboarding
```

The page runs this step on demand. A separate `python -m book_sync.sync.onboarding --watch` mode is available, but do not enable the old timed service alongside the manual page if you want fully on-demand behavior.

Titles narrow candidate EPUBs; metadata alone never establishes a match. Acceptance requires identical normalized reading text, 120 distributed unique context matches in each direction, and at least 95% of 40 coordinate round trips. Quarantined books are retried when the catalog changes. Title metadata with no plausible candidate waits instead of bulk-downloading unrelated documents.

The X4 identity adapter currently assumes the confirmed filename-MD5 mode and Readest downloads named `Title.epub`. Renaming the X4 file or changing its hash mode requires another identity adapter.

Private output:

- `.private/onboarding/ready-profiles.json`: the accepted-profile registry; do not glob old profiles.
- `.private/onboarding/books/<document>/profile.json`: EPUB/KFX fingerprints and validation.
- `.private/onboarding/state.json`: completed library cursor, jobs, retries, and cache fingerprints.
- `.private/manual-sync/`: private before-write snapshots and verification records.
- `.private/web-last-sync.json`: last manual job result.

Automatically generated profiles remain marked `ready_for_dry_run`; this records structural validation, not a per-book physical handoff test. The web page's explicit manual action is the separate authorization to apply a handoff. Source profiles do not enable scheduled writes.

Library pagination must complete before the cursor is committed. Cache changes trigger rebuilds. Removed or newly ambiguous matches leave the ready registry. Only one manual job runs at a time, and every handoff rechecks the connected readers before writing.

To preview a handoff using the same planner as **Sync now**:

```sh
.venv/bin/python -m book_sync.sync.preview
```

## Code layout

Service adapters live in `book_sync/plugins/`. The sync jobs in `book_sync/sync/` match books and decide which positions to update. Shared text and chapter maps live in `book_sync/mapping/`, and the web server and templates live in `book_sync/web/`. Tests and synthetic fixtures are under `tests/`, and systemd units and configuration examples are under `deploy/`.

Add a reader or catalog by installing a plugin package. The [plugin guide](docs/plugins.md) describes the contract and shows how to test a fourth reader.

## Tests and privacy

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The [synthetic test EPUB](tests/fixtures/Book%20Sync%20Progress%20Test.epub) contains synthetic test prose. Real books, reading history, credentials, personal host details, and account-specific experiments are excluded. Optional real-book checks require a local profile supplied through `BOOK_SYNC_TEST_PROFILE`.

See [sharing guide](docs/sharing.md) for the publication boundary. The implementation is a personal prototype, using unofficial service endpoints.

Protocol references: [Kindle registration research](https://github.com/mkb79/kindle), [personal-document KFX delivery](https://github.com/kafkasl/ksync), [Readest reader APIs](https://github.com/readest/readest/tree/main/apps/readest-app/src/app/api/crosspoint), and [CrossPoint Sync API](https://github.com/crosspoint-reader/crosspoint-sync/blob/main/docs/API.md).
