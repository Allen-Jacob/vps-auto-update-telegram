# VPS Auto Update Telegram

Un agent local pour Ubuntu/Debian qui vérifie les mises à jour Ubuntu et les images Docker des services actifs d’un projet Docker Compose. Il attend 24 heures par défaut après la détection d’une version stable, puis envoie une demande Telegram avec les boutons **Installer et redémarrer si nécessaire** et **Ignorer**. Rien n’est installé tant que tu n’as pas appuyé sur le bouton.

Après approbation, il fait une sauvegarde protégée du système et, si des images Docker ont changé, une sauvegarde des données/configurations NetBird avant de mettre à jour. Il simule `apt-get full-upgrade` pour afficher le plan, y compris les suppressions éventuelles, puis l’exécute seulement après approbation. Il met aussi à jour les services Docker actifs du Compose indiqué, vérifie l’API NetBird et l’état des conteneurs, puis redémarre uniquement si Ubuntu crée `/var/run/reboot-required`. Au retour du VPS, Telegram reçoit un message de confirmation.

Exemple de notification :

```text
VPS NETBIRD - UPDATE prête après le délai de stabilité.
2 paquet(s) Ubuntu, 3 service(s) Docker
Installer mettra à jour Ubuntu et les services Docker du fichier Compose...
```

## Installation guidée

Prérequis : Ubuntu/Debian avec systemd, Python 3, Docker Compose v2, un projet Compose NetBird et `git`. Le dépôt est public afin que le clonage ne demande pas de connexion GitHub.

```bash
git clone https://github.com/Allen-Jacob/vps-auto-update-telegram.git && cd vps-auto-update-telegram && sudo bash ./install.sh
```

L’installateur te demande :

1. Le libellé du VPS qui apparaîtra au début de chaque message, par exemple `VPS NETBIRD`.
2. Le chemin du fichier Docker Compose.
3. Le délai de stabilité, 24 heures par défaut.
4. L’URL de contrôle de l’API NetBird, détectée automatiquement depuis `proxy.env` si possible.
5. Réutiliser le token déjà configuré par l’ancien minuteur NetBird, ou saisir le token d’un nouveau bot avec saisie masquée.

Si tu crées un nouveau bot, fais-le avec `@BotFather`, ouvre-le dans Telegram et envoie `/start`. L’installateur vérifie le bot, identifie ton chat privé, envoie un message de test, puis active les services. Il désactive l’ancien minuteur automatique NetBird seulement après le succès du test pour éviter deux mécanismes de mise à jour concurrents.

Le token et les identifiants Telegram sont écrits seulement dans `/etc/vps-update-agent/telegram.env` avec les permissions `0600`. Ils ne sont ni demandés en argument de commande ni enregistrés dans GitHub. Les boutons sont acceptés uniquement depuis le compte Telegram et le chat enregistrés pendant l’installation.

## Comportement

- Vérifie toutes les six heures les mises à jour APT et les images des services Compose actifs qui ont une image distante.
- Envoie une première alerte quand une mise à jour apparaît. Si la liste reste identique pendant le délai configuré, envoie le bouton d’approbation.
- Si l’image ou les paquets changent, le délai repart. **Ignorer** écarte cette liste jusqu’à ce qu’elle change.
- Le bouton **Installer** vérifie à nouveau la liste avant d’agir. Si elle a changé depuis la notification, l’ancienne approbation est rejetée et le délai repart.
- La sauvegarde système conserve `/etc`, l’état APT et la sélection des paquets dans `/var/backups/vps-update`, avec permissions `0600`. La dernière est gardée; les précédentes sont supprimées après 24 heures.
- Si des images NetBird/CrowdSec changent, `vps-update-backup.py` sauvegarde les données et configurations NetBird/CrowdSec dans `/var/backups/netbird`, en arrêtant brièvement les services nécessaires à la cohérence SQLite.
- Les fichiers de sauvegarde sont sur le même VPS et contiennent des secrets; ils ne remplacent pas une sauvegarde hors site.
- Le plan `full-upgrade` présente les suppressions éventuelles dans la demande d’approbation, avant que tu choisisses Installer. Une sauvegarde système protégée est créée avant l’installation.
- Si une commande échoue, l’agent t’envoie une alerte, garde les sauvegardes et bloque la répétition automatique de cette même liste.

## Vérification et journaux

```bash
systemctl status vps-update-agent.service vps-update-scan.timer
journalctl -u vps-update-agent.service -u vps-update-scan.service -n 100 --no-pager
ls -lh /var/backups/vps-update /var/backups/netbird
```

Pour changer le libellé, le chemin Compose ou le délai, modifie `/etc/vps-update-agent/config.env`. Le bouton garde les droits au compte Telegram identifié lors de l’installation.

## Limites

Ce dépôt ne peut pas revenir automatiquement à l’ancienne version Ubuntu ni restaurer une base NetBird après une mise à jour ratée. L’alerte indiquera l’échec et le chemin de la sauvegarde pour une restauration manuelle. Les sauvegardes restent locales; configure aussi une sauvegarde externe si tu veux te protéger contre la perte du VPS.
