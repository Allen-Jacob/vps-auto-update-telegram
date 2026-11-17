# NetBird VPS auto-update

A small systemd-based updater for a self-hosted NetBird Docker Compose stack. It checks for changed images every six hours, waits 24 hours after first detection, makes a root-only backup, updates NetBird, checks the API, and sends Telegram notifications.

Messages use a configurable prefix, for example:

```text
VPS NETBIRD - CONTAINER update détectée pour NetBird; délai de sécurité de 24 h commencé.
VPS NETBIRD - CONTAINER update terminée pour NetBird; services et API vérifiés.
```

The original `containrrr/watchtower` repository was archived in December 2025. This project uses a local systemd service instead, so it does not add a container with access to the Docker socket.

## What it updates

By default, it updates only these Compose services:

- `netbird-server`
- `dashboard`
- `proxy`

It does not update CrowdSec, Traefik, or other containers. To change the targets, edit `TARGETS` in `/etc/netbird-auto-update/config.env`.

The updater pulls the configured image references and records the candidate image IDs. The full set must remain unchanged for `GRACE_HOURS` (24 by default); any changed ID restarts the timer. The six-hour poll interval means installation happens at least 24 hours after first detection, plus up to one poll interval.

Before applying an update, the backup script briefly stops `netbird-server` and CrowdSec when present so SQLite and CrowdSec data can be copied consistently. It archives the Compose/config files and the NetBird data volume. Archives are stored under `/var/backups/netbird`, mode `0600`; the newest is retained indefinitely and older ones are pruned after 24 hours. These archives contain secrets and stay on the same VPS unless you copy them elsewhere.

After updating, the service checks that every target container is running and that `HEALTHCHECK_URL` returns HTTP 200 or 401 over valid HTTPS. On failure it sends an alert and keeps the backup. It does not automatically restore the database; review the journal and restore manually if needed.

## Install

Requirements: Ubuntu/Debian with systemd, Docker Compose v2, Python 3, and a NetBird Compose deployment.

```bash
git clone https://github.com/OWNER/netbird-vps-auto-updater.git
cd netbird-vps-auto-updater
sudo ./install.sh
sudo nano /etc/netbird-auto-update/config.env
```

Set at least:

- `VPS_LABEL="VPS NETBIRD"` (choose the prefix for Telegram messages)
- `COMPOSE_FILE=/path/to/docker-compose.yml`
- `HEALTHCHECK_URL=https://your-netbird-domain/api/instance/version`

Keep the service names in `TARGETS` aligned with your Compose file. Do not put Telegram tokens in this repo or in `config.env`.

## Telegram setup

1. In Telegram, use the official `@BotFather` and send `/newbot`.
2. Choose a display name and a unique username ending in `bot`.
3. Open your new bot and send `/start`.
4. On the VPS, run `sudo /usr/local/sbin/netbird-telegram-setup.py`. Paste the BotFather token at the hidden prompt.

The setup script discovers the private chat ID, writes the token to `/etc/netbird-auto-update/telegram.env` with mode `0600`, sends a test message, and enables the timer only after that message succeeds.

## Operations

```bash
systemctl list-timers netbird-auto-update.timer
systemctl status netbird-auto-update.timer
journalctl -u netbird-auto-update.service -n 100 --no-pager
ls -lh /var/backups/netbird
```

The first successful check only records an update candidate. It does not update immediately. A failed candidate is blocked from repeated automatic attempts; a new image candidate starts a fresh 24-hour window.
