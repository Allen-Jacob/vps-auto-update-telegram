#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Relance avec sudo: sudo ./install.sh" >&2
  exit 1
fi

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for command in python3 systemctl docker; do
  command -v "$command" >/dev/null || { echo "Commande requise absente: $command" >&2; exit 1; }
done
docker compose version >/dev/null
systemctl is-system-running >/dev/null 2>&1 || true

install -d -m 0700 /etc/vps-update-agent /var/lib/vps-update-agent /var/backups/netbird /var/backups/vps-update
if [[ -f /etc/netbird-auto-update/config.env ]]; then
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  cp -a /etc/netbird-auto-update/config.env "/root/netbird-update-config-${stamp}.bak"
  chmod 0600 "/root/netbird-update-config-${stamp}.bak"
fi

install -m 0700 "$repo_dir/vps-update-agent.py" /usr/local/sbin/vps-update-agent.py
install -m 0700 "$repo_dir/setup-telegram.py" /usr/local/sbin/setup-vps-update-telegram.py
install -m 0700 "$repo_dir/vps-update-backup.py" /usr/local/sbin/vps-update-backup.py
install -m 0644 "$repo_dir/systemd/vps-update-agent.service" /etc/systemd/system/vps-update-agent.service
install -m 0644 "$repo_dir/systemd/vps-update-scan.service" /etc/systemd/system/vps-update-scan.service
install -m 0644 "$repo_dir/systemd/vps-update-scan.timer" /etc/systemd/system/vps-update-scan.timer

echo "Le token sera saisi de façon masquée. Avant de continuer, crée un bot avec @BotFather puis envoie /start à ce nouveau bot dans Telegram."
python3 /usr/local/sbin/setup-vps-update-telegram.py

# Only replace the old automatic Docker updater after the new bot has passed its Telegram test.
systemctl daemon-reload
systemctl enable --now vps-update-agent.service
systemctl is-active --quiet vps-update-agent.service
if systemctl is-active --quiet netbird-auto-update.service; then
  echo "Attente de la fin de l'ancien contrôle NetBird avant de le désactiver..."
  while systemctl is-active --quiet netbird-auto-update.service; do sleep 5; done
fi
systemctl disable --now netbird-auto-update.timer 2>/dev/null || true
systemctl enable --now vps-update-scan.timer
systemctl start vps-update-scan.service

echo
echo "Installation terminée. L’ancien minuteur NetBird automatique est désactivé."
echo "Le nouveau contrôle vérifie Ubuntu et les services Docker actifs de ce fichier Compose toutes les 6 h."
echo "Il attend 24 h (ou le délai choisi), puis demande ton accord Telegram avant d’installer."
echo "Commandes: systemctl status vps-update-agent.service vps-update-scan.timer"
echo "Journaux: journalctl -u vps-update-agent.service -u vps-update-scan.service -n 100 --no-pager"
