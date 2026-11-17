#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo: sudo ./install.sh" >&2
  exit 1
fi

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -d -m 0700 /etc/netbird-auto-update
install -d -m 0700 /var/lib/netbird-auto-update
install -d -m 0700 /var/backups/netbird
install -m 0700 "$repo_dir/netbird-auto-update.py" /usr/local/sbin/netbird-auto-update.py
install -m 0700 "$repo_dir/netbird-backup.py" /usr/local/sbin/netbird-backup.py
install -m 0700 "$repo_dir/netbird-telegram-setup.py" /usr/local/sbin/netbird-telegram-setup.py
install -m 0644 "$repo_dir/systemd/netbird-auto-update.service" /etc/systemd/system/netbird-auto-update.service
install -m 0644 "$repo_dir/systemd/netbird-auto-update.timer" /etc/systemd/system/netbird-auto-update.timer

if [[ ! -f /etc/netbird-auto-update/config.env ]]; then
  install -m 0600 "$repo_dir/config.env.example" /etc/netbird-auto-update/config.env
  echo "Created /etc/netbird-auto-update/config.env; edit VPS_LABEL and HEALTHCHECK_URL before enabling updates."
fi

systemctl daemon-reload
printf '\nInstalled. The timer is not enabled until Telegram is tested.\n'
printf '1. Edit /etc/netbird-auto-update/config.env.\n'
printf '2. Send /start to your Telegram bot.\n'
printf '3. Run: sudo /usr/local/sbin/netbird-telegram-setup.py\n'
