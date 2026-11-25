#!/usr/bin/env python3
"""Collect Telegram credentials without echoing or logging the bot token."""
import getpass
import json
import os
import pathlib
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

DEST = pathlib.Path('/etc/vps-update-agent/telegram.env')
CONFIG = pathlib.Path('/etc/vps-update-agent/config.env')


def call(token, method, fields=None, timeout=15):
    url = 'https://api.telegram.org/bot' + token + '/' + method
    data = urllib.parse.urlencode(fields).encode() if fields is not None else None
    req = urllib.request.Request(url, data=data, method='POST' if data else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError('Telegram HTTP ' + str(exc.code)) from None
    except Exception as exc:
        raise RuntimeError('Telegram connection failed (' + type(exc).__name__ + ')') from None
    if not result.get('ok'):
        raise RuntimeError('Telegram rejected the request: ' + str(result.get('description', 'unknown error')))
    return result.get('result')


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo: sudo /usr/local/sbin/setup-telegram.py')
    label = input('Nom du VPS affiché dans Telegram [VPS NETBIRD]: ').strip() or 'VPS NETBIRD'
    compose = input('Chemin du fichier Docker Compose [/root/docker-compose.yml]: ').strip() or '/root/docker-compose.yml'
    if any(ch in label for ch in '\r\n=') or any(ch in compose for ch in '\r\n'):
        raise SystemExit('Les champs ne peuvent pas contenir de retour à la ligne.')
    if not pathlib.Path(compose).is_file():
        raise SystemExit('Fichier Docker Compose introuvable: ' + compose)
    result = subprocess.run(['docker', 'compose', '-f', compose, 'config', '--quiet'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise SystemExit('Le fichier Docker Compose est invalide: ' + result.stderr[-600:])
    proxy_env = pathlib.Path(compose).parent / 'proxy.env'
    detected_domain = ''
    if proxy_env.is_file():
        for line in proxy_env.read_text().splitlines():
            if line.strip().startswith('NB_PROXY_DOMAIN='):
                detected_domain = line.split('=', 1)[1].strip().strip('"').strip("'").rstrip('/')
                if detected_domain and not detected_domain.startswith(('http://', 'https://')):
                    detected_domain = 'https://' + detected_domain
                if detected_domain:
                    detected_domain += '/api/instance/version'
                break
    health = input('URL API NetBird [' + (detected_domain or 'requis si aucun proxy.env') + ']: ').strip() or detected_domain
    if any(ch in health for ch in '\r\n'):
        raise SystemExit('URL invalide.')
    if not health:
        raise SystemExit('Une URL API NetBird est requise pour vérifier l’application après mise à jour.')
    try:
        grace = int(input('Délai de stabilité en heures [24]: ').strip() or '24')
    except ValueError:
        raise SystemExit('Le délai doit être un nombre entier d’heures.')
    if grace < 1 or grace > 720:
        raise SystemExit('Le délai doit être entre 1 et 720 heures.')
    token = getpass.getpass('Token Telegram du nouveau bot (saisie masquée): ').strip()
    if not token or ':' not in token:
        raise SystemExit('Token invalide.')
    me = call(token, 'getMe')
    print('Bot vérifié: @' + me['username'])
    print('Ouvre ce bot dans Telegram, envoie /start, puis reviens ici.')
    input('Quand /start est envoyé, appuie sur Entrée...')
    chats = []
    deadline = time.time() + 90
    offset = None
    while time.time() < deadline:
        fields = {'timeout': 5, 'allowed_updates': json.dumps(['message'])}
        if offset is not None:
            fields['offset'] = offset
        try:
            updates = call(token, 'getUpdates', fields, timeout=10) or []
        except RuntimeError as exc:
            raise SystemExit(str(exc) + '\nVérifie que ce bot n’a pas de webhook configuré.')
        for update in updates:
            offset = update['update_id'] + 1
            message = update.get('message', {})
            chat = message.get('chat', {})
            sender = message.get('from', {})
            if (message.get('text', '').split()[0:1] == ['/start']
                    and chat.get('type') == 'private'
                    and sender.get('id') == chat.get('id')):
                chats.append((str(chat['id']), str(sender['id'])))
        if chats:
            break
    if not chats:
        raise SystemExit('Aucun /start privé trouvé après 90 s. Relance le script après avoir envoyé /start au bot.')
    chat_id, user_id = chats[-1]
    CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_text = (f'VPS_LABEL={label}\nCOMPOSE_FILE={compose}\nGRACE_HOURS={grace}\n'
                   f'HEALTHCHECK_URL={health}\nMESSAGE_PREFIX=UPDATE\n')
    CONFIG.write_text(config_text)
    os.chmod(CONFIG, 0o600)
    temp = DEST.with_suffix('.tmp')
    temp.write_text(f'BOT_TOKEN={token}\nCHAT_ID={chat_id}\nUSER_ID={user_id}\n')
    os.chmod(temp, 0o600)
    os.replace(temp, DEST)
    try:
        call(token, 'sendMessage', {'chat_id': chat_id,
             'text': f'{label} - UPDATE: bot configuré; je surveillerai Ubuntu et Docker. Les installations demanderont ton approbation.'})
    except Exception:
        DEST.unlink(missing_ok=True)
        raise
    print('Configuration Telegram testée et enregistrée avec les permissions 600.')


if __name__ == '__main__':
    main()
