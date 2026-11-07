# Run Placekeeper as a Linux service

This guide installs Placekeeper on a Linux host with systemd, such as DietPi. For a quick trial from a terminal, use the [README](../README.md). Run the commands below as root, or use `sudo` for the administrative commands.

## 1. Install the app and create its data directory

On Debian 13, install Git and Python 3.13 with its virtual environment module:

```sh
apt update
apt install git python3 python3-venv
```

For a fresh installation:

```sh
useradd --system --user-group --home-dir /var/lib/placekeeper --shell /usr/sbin/nologin placekeeper
install -d -o placekeeper -g placekeeper -m 700 /var/lib/placekeeper
git clone https://github.com/adityakalro/placekeeper.git /opt/placekeeper
cd /opt/placekeeper
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-onboarding.txt
ln -s /var/lib/placekeeper .private
install -o placekeeper -g placekeeper -m 600 deploy/web-config.example.json .private/web-config.json
```

The service runs as `placekeeper`. Keep the app and virtual environment under `/opt/placekeeper`, and store credentials and book maps under `/var/lib/placekeeper`.

From `/opt/placekeeper`, open a shell as the service user:

```sh
runuser -u placekeeper -- sh
```

In that shell, run `install -d -m 700 .private/vendor`, then follow the [KFX parser installation commands](../README.md#1-install-placekeeper). Exit the shell when finished. If PyPI is unavailable on the host, transfer wheels that match its Python version and CPU architecture and install with `pip install --no-index --find-links /path/to/wheels -r requirements-onboarding.txt`.

## 2. Start the service and connect accounts

```sh
install -m 644 /opt/placekeeper/deploy/placekeeper-web.service /etc/systemd/system/placekeeper-web.service
systemctl daemon-reload
systemctl enable --now placekeeper-web.service
systemctl status placekeeper-web.service --no-pager
```

The example configuration listens on localhost. On your computer, keep this SSH tunnel open:

```sh
ssh -N -L 127.0.0.1:8780:127.0.0.1:8780 YOUR_USER@YOUR_PI_HOST
```

Open [Setup](http://127.0.0.1:8780/setup), [connect your accounts](../README.md#3-connect-your-accounts), and [sync a first book](../README.md#4-sync-your-first-book). The web page starts at boot. Use **Find new books** for discovery and mapping and **Sync now** for progress. To sync progress every 30 minutes, enable automatic progress sync in **Setup**. The web service saves the setting across restarts and starts a fresh interval on startup. A scheduled run skips its turn if another job is active or the necessary services are disconnected.

## 3. Optional LAN or WireGuard access

Edit `/var/lib/placekeeper/web-config.json`. Set `bind` to a private interface address, or use `0.0.0.0` for multiple interfaces. Keep the trusted client ranges in `networks` and list each address you actually visit in `hosts`, including its port. For example, replace the example LAN address and subnet below with your own. Add your WireGuard address and subnet if you use it.

```json
{
  "bind": "0.0.0.0",
  "port": 8780,
  "hosts": ["127.0.0.1:8780", "192.168.1.20:8780"],
  "networks": ["127.0.0.0/8", "192.168.1.0/24"]
}
```

Run `systemctl restart placekeeper-web.service` after changing the file. Use a trusted private LAN, SSH tunnel, or WireGuard. Keep port 8780 off public router forwards. The page shares the installation’s accounts with clients allowed by its network rules.

If the page fails to start, check `journalctl -u placekeeper-web.service -n 30 --no-pager`. A **Private network required** or access-denied response usually means the address or client subnet is missing from the configuration.

## Upgrading from Book Sync

Stop the old web service and, if installed, the optional onboarding service
before copying the saved data. Use the fresh installation commands above to
create the Placekeeper service account and checkout. Keep a copy of the old data
until you have verified the upgrade.

```sh
systemctl disable --now book-sync-web.service
# Run this if the old onboarding unit was installed.
systemctl disable --now book-sync-onboarding.service
```

Copy the old private data and update its saved paths before starting Placekeeper.
This also carries over the web configuration and progress schedule.

```sh
cp -a /var/lib/book-sync/. /var/lib/placekeeper/
chown -R placekeeper:placekeeper /var/lib/placekeeper
cd /opt/placekeeper
runuser -u placekeeper -- .venv/bin/python -m deploy.relocate \
  --data-root /var/lib/placekeeper --previous-root /var/lib/book-sync
```

Follow the service startup commands above and verify the Sync and Setup pages
using your existing address.

## Moving an existing installation

Transfer the saved private data over SSH, preserve file permissions, and ensure the service user owns it. Existing accounts can be reused. Relocate map paths and registry fingerprints before starting the service:

```sh
cd /opt/placekeeper
runuser -u placekeeper -- .venv/bin/python -m deploy.relocate \
  --data-root /var/lib/placekeeper --previous-root /path/to/old/private
```

Verify account access, a fresh download, and a manual sync on the new host before retiring the old installation. Keep personal SSH addresses and connection notes in the ignored local `DEPLOYMENT.md`.
