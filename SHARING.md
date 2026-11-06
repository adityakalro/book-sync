# What belongs in GitHub

Shareable files:

- Generic Python implementation and synthetic-data tests.
- Dependency manifests, service unit templates, and a localhost-only config example.
- Generic setup/deployment instructions and protocol-source references.
- The explicitly included synthetic test EPUB.

Keep private and ignored:

- `.private/`: all Amazon, Readest, and CrossPoint credentials; password-equivalent
  hashes; OAuth callbacks and pending device grants; signed download links.
- Real EPUB/KFX/MOBI files, extracted book text, reading positions, annotations,
  highlights, account exports, logs, profiles, caches, and onboarding state.
- `DEPLOYMENT.md`: personal SSH addresses, device details, and connection notes.
- Account-specific experimental helpers and their tests, which remain local.
- Virtual environments, third-party parser copies, downloaded wheels, and bundles.

The `.gitignore` is a backup guard, not a substitute for reviewing the exact
staged snapshot. Never force-add private artifacts. Generic protocol field names
and clearly fake test tokens are not credentials. Real tokens must never enter
Git history, including private repositories.

The KFX parser is an external GPLv3 component and is not redistributed here.
Retain its source/license when deploying it separately. This repository contains
no real book content and makes no claim to redistribute copyrighted ebooks.

Before publishing or changing repository visibility, inspect `git diff --cached`
and run the tests from an exported clean snapshot. A private GitHub repository
is the default publication target for this personal prototype.
