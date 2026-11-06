# Book Sync

Self-hosted book onboarding and manual reading-progress sync between Kindle personal documents and an X4 running CrossPoint. Designed for a small ARM64 Linux host such as DietPi.

The private web page has a **Sync now** button. A click discovers new books, matches Readest cloud EPUBs to processed Send to Kindle documents, validates their maps, and applies forward-only progress handoffs. There is no scheduled reading-progress sync. Refreshing the page never starts a job.

## What it supports

- Direct Amazon personal-document discovery and KFX downloads through a registered Kindle-compatible test client.
- Readest reader-device pairing and cloud EPUB downloads without extracting browser cookies.
- Exact normalized full-text matching, unique context checks, and map round trips. Ambiguous or different editions are held for review.
- Compact text coordinates suitable for a low-memory Raspberry Pi.
- Manual bidirectional handoffs, fresh-read guards, readback verification, and no automatic retry after an uncertain write.
- A responsive LAN/WireGuard page with background job status and duplicate-click prevention.

Progress is approximate to a Kindle location bucket, not guaranteed to the exact paragraph. The coordinate bridge was experimentally validated for Send to Kindle documents; Amazon purchases, audiobooks, and other document formats are not validated. Cloud endpoints are unofficial and may change.

## Setup

Use Python 3.13 and a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-onboarding.txt
```

The KFX parser is an external GPLv3 dependency and is **not vendored in this repository**. Supply an unmodified `kfxlib` source tree plus its bundled plugin modules, such as the one in [kindle.koplugin](https://github.com/kaikozlov/kindle.koplugin/tree/501488fd99ace3ea143a8693b9fa7c6421101d51/python). Set `--kfxlib-parent` when running onboarding, or place it at `.private/vendor/python`. Retain its license and source when deploying it.

Create private, mode-600 credential files under `.private/`:

- `amazon-device.json`: signed Amazon device credentials for progress reads/writes.
- `amazon-kindle-download-device.json`: Kindle-compatible credentials for discovery and KFX delivery.
- `readest-device.json`: reader key produced by Readest pairing.

`kindle_download_device.py` prepares the download-device registration plan. Creating an Amazon device grants account access and must be explicitly approved by the account owner. Its `--login` mode provides a localhost sign-in helper using an Amazon authorization code with PKCE. Existing Amazon sessions can be too old for registration even after token refresh.

Readest pairing:

```sh
.venv/bin/python readest_source.py pair
# Approve the displayed reader code in your Readest account, then:
.venv/bin/python readest_source.py finish
```

Readest grants a reader key library/statistics/progress access; this connector uses its catalog and file endpoints only. An EPUB must be uploaded to Readest Cloud, not only stored locally in an app.

## Run the page

Copy `web-config.example.json` to `.private/web-config.json` and configure the private interface addresses you will use. The default example permits localhost only. See [DEPLOYMENT.example.md](DEPLOYMENT.example.md) for a Linux service layout.

```sh
.venv/bin/python sync_page.py
```

Open the page, connect the CrossPoint sync account used on the X4, and explicitly approve saving the password-equivalent API key on the host. The plaintext password is not saved. Later visits need only **Sync now**.

Before clicking, sync the reader you were using. Afterward, use **Sync Progress** on the X4 or **Sync to Furthest Page Read** on Kindle to pick up the cloud update. Newly mapped books need an initial progress record on both services. Intentional rewinds are not implemented.

Serve this only on a private LAN or through WireGuard. Do not port-forward it onto the public Internet. Network and Host checks plus same-origin/CSRF checks are built in; it is not a public multi-user authentication system. A localhost SSH tunnel is useful for the initial credential form.

## Onboarding and maps

```sh
.venv/bin/python onboarding.py
```

The page runs this step on demand. A separate `onboarding.py --watch` mode is available, but do not enable the old timed service alongside the manual page if you want fully on-demand behavior.

Titles narrow candidate EPUBs; metadata alone never establishes a match. Acceptance requires identical normalized reading text, 120 distributed unique context matches in each direction, and at least 95% of 40 coordinate round trips. Quarantined books are retried when the catalog changes. Title metadata with no plausible candidate waits instead of bulk-downloading unrelated documents.

The X4 identity adapter currently assumes the confirmed filename-MD5 mode and Readest downloads named `Title.epub`. Renaming the X4 file or changing its hash mode requires another identity adapter.

Private output:

- `.private/onboarding/ready-profiles.json`: the accepted-profile registry; do not glob old profiles.
- `.private/onboarding/books/<document>/profile.json`: EPUB/KFX fingerprints and validation.
- `.private/onboarding/state.json`: completed library cursor, jobs, retries, and cache fingerprints.
- `.private/manual-sync/`: private before-write snapshots and verification records.
- `.private/web-last-sync.json`: last manual job result.

Automatically generated profiles remain marked `ready_for_dry_run`; this records structural validation, not a per-book physical handoff test. The web page's explicit manual action is the separate authorization to apply a handoff. Source profiles do not enable scheduled writes.

Library pagination must complete before the cursor is committed. Cache changes trigger rebuilds. Removed or newly ambiguous matches leave the ready registry. Only one manual job runs at a time, and every handoff rechecks both positions before writing.

The standalone read-only poller remains available:

```sh
.venv/bin/python sync_dry_run.py --profile /path/to/private/profile.json --login terminal
```

## Tests and privacy

```sh
.venv/bin/python -m unittest discover -v
```

The included **Book Sync Progress Test.epub** contains synthetic test prose. Real books, reading history, credentials, personal host details, and account-specific experiments are excluded. Optional real-book checks require a local profile supplied through `BOOK_SYNC_TEST_PROFILE`.

See [SHARING.md](SHARING.md) for the publication boundary. The implementation is a personal prototype, not an Amazon, Readest, or CrossPoint supported integration.

Protocol references: [Kindle registration research](https://github.com/mkb79/kindle), [personal-document KFX delivery](https://github.com/kafkasl/ksync), [Readest reader APIs](https://github.com/readest/readest/tree/main/apps/readest-app/src/app/api/crosspoint), and [CrossPoint Sync API](https://github.com/crosspoint-reader/crosspoint-sync/blob/main/docs/API.md).
