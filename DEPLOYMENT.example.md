# Linux deployment example

Personal connection details belong in an ignored local `DEPLOYMENT.md`.
Use your own SSH host and username; no credentials should be entered in this file.

The supplied `book-sync-web.service` expects:

- Code and virtual environment at `/opt/book-sync`.
- A dedicated `book-sync` user and group.
- Private data at `/var/lib/book-sync`, mode 700, owned by `book-sync`.
- A symlink from `/opt/book-sync/.private` to `/var/lib/book-sync`.
- Existing Amazon and Readest credential files transferred privately, mode 600.
- The external KFX parser at `.private/vendor/python`.

Install `requirements-onboarding.txt` inside the virtual environment. For a host
that cannot reach PyPI, download compatible wheels on another machine and use
`pip install --no-index --find-links /path/to/wheels -r requirements-onboarding.txt`.
Use wheel tags appropriate to the destination Python version and ARM architecture.

Existing maps and state can be transferred without repeating account pairing.
Relocate their paths and registry fingerprints before starting the new service:

```sh
/opt/book-sync/.venv/bin/python /opt/book-sync/dietpi_relocate.py \
  --data-root /var/lib/book-sync --previous-root /path/to/old/private
```

Copy `web-config.example.json` to the private data directory. Add the LAN and
WireGuard IP:port pairs you will use to `hosts`, and the corresponding trusted
client subnets to `networks`. Set `bind` appropriately. Keep access private;
do not add a public router port forward. The WireGuard tunnel supplies encryption
for remote access. An SSH tunnel can protect initial setup from a local computer:

```sh
ssh -N -L 127.0.0.1:8780:127.0.0.1:8780 YOUR_SSH_HOST
```

After testing, install and enable `book-sync-web.service` with systemd.
Disable the old `book-sync-onboarding.service` if previously installed. The web
service starts at boot but runs discovery/progress updates only after a click.

```sh
systemctl enable --now book-sync-web.service
systemctl status book-sync-web.service --no-pager
journalctl -u book-sync-web.service -n 20 --no-pager
```

Verify account access, a fresh download, map checks, and a manual sync on the
destination before shutting down the old host. Preserve private rollback copies.

The compact coordinate representation avoids millions of retained Python XPath
strings on low-memory hosts. Full-book validation still takes CPU time; the
service runs at reduced CPU/I/O priority and processes books one at a time.
