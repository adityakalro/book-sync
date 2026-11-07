# Run Book Sync as a Linux service

This guide installs Book Sync on a Linux host with systemd, such as DietPi. For a quick trial from a terminal, use the [README](../README.md). Run the commands below as root, or use `sudo` for the administrative commands.

## 1. Install the app and create its data directory

On Debian 13, install Git and Python 3.13 with its virtual environment module:

```sh
apt update
apt install git python3 python3-venv
```

For a fresh installation:

```sh
useradd --system --user-group --home-dir /var/lib/book-sync --shell /usr/sbin/nologin book-sync
install -d -o book-sync -g book-sync -m 700 /var/lib/book-sync
git clone https://github.com/adityakalro/book-sync.git /opt/book-sync
cd /opt/book-sync
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-onboarding.txt
ln -s /var/lib/book-sync .private
install -o book-sync -g book-sync -m 600 deploy/web-config.example.json .private/web-config.json
```

The service runs as `book-sync`. Keep the app and virtual environment under `/opt/book-sync`, and store credentials and book maps under `/var/lib/book-sync`.

From `/opt/book-sync`, open a shell as the service user:

```sh
runuser -u book-sync -- sh
```

In that shell, run `install -d -m 700 .private/vendor`, then follow the [KFX parser installation commands](../README.md#1-install-book-sync). Exit the shell when finished. If PyPI is unavailable on the host, transfer wheels that match its Python version and CPU architecture and install with `pip install --no-index --find-links /path/to/wheels -r requirements-onboarding.txt`.

## 2. Start the service and connect accounts

```sh
install -m 644 /opt/book-sync/deploy/book-sync-web.service /etc/systemd/system/book-sync-web.service
systemctl daemon-reload
systemctl enable --now book-sync-web.service
systemctl status book-sync-web.service --no-pager
```

The example configuration listens on localhost. On your computer, keep this SSH tunnel open:

```sh
ssh -N -L 127.0.0.1:8780:127.0.0.1:8780 YOUR_USER@YOUR_PI_HOST
```

Open [Setup](http://127.0.0.1:8780/setup), [connect your accounts](../README.md#3-connect-your-accounts), and [sync a first book](../README.md#4-sync-your-first-book). The web page starts at boot. Use **Find new books** for discovery and mapping and **Sync now** for progress. To sync progress every 30 minutes, enable automatic progress sync in **Setup**. The web service saves the setting across restarts and starts a fresh interval on startup. A scheduled run skips its turn if another job is active or the necessary services are disconnected. If you previously enabled the optional five-minute onboarding service, disable it with `sudo systemctl disable --now book-sync-onboarding.service` to run discovery from the page.

## 3. Optional LAN or WireGuard access

Edit `/var/lib/book-sync/web-config.json`. Set `bind` to a private interface address, or use `0.0.0.0` for multiple interfaces. Keep the trusted client ranges in `networks` and list each address you actually visit in `hosts`, including its port. For example, replace the example LAN address and subnet below with your own. Add your WireGuard address and subnet if you use it.

```json
{
  "bind": "0.0.0.0",
  "port": 8780,
  "hosts": ["127.0.0.1:8780", "192.168.1.20:8780"],
  "networks": ["127.0.0.0/8", "192.168.1.0/24"]
}
```

Run `systemctl restart book-sync-web.service` after changing the file. Use a trusted private LAN, SSH tunnel, or WireGuard. Keep port 8780 off public router forwards. The page shares the installation’s accounts with clients allowed by its network rules.

If the page fails to start, check `journalctl -u book-sync-web.service -n 30 --no-pager`. A **Private network required** or access-denied response usually means the address or client subnet is missing from the configuration.

## Upgrading an installation with the old file layout

After updating the code, reinstall the service unit from `deploy/`, run
`systemctl daemon-reload`, and restart Book Sync. The new unit starts the
`book_sync.web.app` module from `/opt/book-sync`. If you enabled the optional
onboarding service, reinstall its unit from `deploy/` as well. Keep the existing
`/var/lib/book-sync` directory and `.private` link so the new version uses the
same accounts and book maps.

## Moving an existing installation

Transfer the saved private data over SSH, preserve file permissions, and ensure the service user owns it. Existing accounts can be reused. Relocate map paths and registry fingerprints before starting the service:

```sh
cd /opt/book-sync
runuser -u book-sync -- .venv/bin/python -m deploy.relocate \
  --data-root /var/lib/book-sync --previous-root /path/to/old/private
```

Verify account access, a fresh download, and a manual sync on the new host before retiring the old installation. Keep personal SSH addresses and connection notes in the ignored local `DEPLOYMENT.md`.
