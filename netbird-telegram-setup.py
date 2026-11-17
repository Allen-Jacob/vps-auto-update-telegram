#!/usr/bin/env python3
"""Securely save a Telegram bot token, find the private chat, test, and enable timer."""
import getpass
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

CONFIG_FILE = pathlib.Path('/etc/netbird-auto-update/config.env')
DEST = pathlib.Path('/etc/netbird-auto-update/telegram.env')

def label():
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            if line.startswith('VPS_LABEL='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    return 'VPS NETBIRD'

def api(token, method, fields=None):
    url = 'https://api.telegram.org/bot' + token + '/' + method
    data = urllib.parse.urlencode(fields or {}).encode() if fields is not None else None
    request = urllib.request.Request(url, data=data, method='POST' if data else 'GET')
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError('Telegram returned HTTP ' + str(error.code)) from None
    except Exception:
        raise RuntimeError('Could not reach Telegram API') from None
    if not result.get('ok'):
        raise RuntimeError('Telegram rejected the request')
    return result

def main():
    if os.geteuid() != 0:
        raise RuntimeError('Run this setup with sudo or as root')
    token = getpass.getpass('BotFather token (hidden): ').strip()
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise RuntimeError('Token format is not valid')
    updates = api(token, 'getUpdates')
    chats = []
    for item in updates.get('result', []):
        message = item.get('message') or item.get('my_chat_member') or {}
        chat = message.get('chat') or {}
        if chat.get('type') == 'private' and chat.get('id') is not None:
            chat_id = str(chat['id'])
            if chat_id not in chats:
                chats.append(chat_id)
    if not chats:
        raise RuntimeError('No private chat found. Send /start to the new bot, then run this setup again.')
    if len(chats) == 1:
        chat_id = chats[0]
    else:
        print('Private chat IDs found: ' + ', '.join(chats))
        chat_id = input('Enter the chat ID for NetBird alerts: ').strip()
        if chat_id not in chats:
            raise RuntimeError('That chat ID was not returned by this bot')
    DEST.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(DEST, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as config:
        config.write('BOT_TOKEN=' + token + chr(10) + 'CHAT_ID=' + chat_id + chr(10))
    os.chmod(DEST, 0o600)
    os.chown(DEST, 0, 0)
    try:
        api(token, 'sendMessage', {'chat_id': chat_id, 'text': label() + ' - Telegram test message delivered.'})
    except Exception:
        DEST.unlink(missing_ok=True)
        raise
    subprocess.run(['systemctl', 'enable', '--now', 'netbird-auto-update.timer'], check=True)
    print('Telegram test message delivered; NetBird auto-update timer enabled.')

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('SETUP_ERROR:', str(error), file=sys.stderr)
        sys.exit(1)
