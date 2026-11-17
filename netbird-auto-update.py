#!/usr/bin/env python3
"""Age-gated Docker Compose updater for a self-hosted NetBird stack."""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_FILE = pathlib.Path('/etc/netbird-auto-update/config.env')
TELEGRAM_FILE = pathlib.Path('/etc/netbird-auto-update/telegram.env')
STATE_DIR = pathlib.Path('/var/lib/netbird-auto-update')
STATE_FILE = STATE_DIR / 'state.json'

def settings():
    values = dict(os.environ)
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    compose_file = pathlib.Path(values.get('COMPOSE_FILE', '/root/docker-compose.yml'))
    targets = tuple(x.strip() for x in values.get('TARGETS', 'netbird-server,dashboard,proxy').split(',') if x.strip())
    grace = max(1, int(values.get('GRACE_HOURS', '24'))) * 3600
    label = values.get('VPS_LABEL', 'VPS NETBIRD').strip()
    health_url = values.get('HEALTHCHECK_URL', '').strip()
    if not health_url:
        proxy_env = compose_file.parent / 'proxy.env'
        for line in proxy_env.read_text().splitlines():
            if line.startswith('NB_PROXY_DOMAIN='):
                host = line.split('=', 1)[1].strip().strip('"').strip("'")
                health_url = host.rstrip('/') + '/api/instance/version'
                if not health_url.startswith(('http://', 'https://')):
                    health_url = 'https://' + health_url
                break
    if not health_url:
        raise RuntimeError('Set HEALTHCHECK_URL in config.env or provide NB_PROXY_DOMAIN in proxy.env')
    if not health_url.endswith('/api/instance/version'):
        health_url = health_url.rstrip('/') + '/api/instance/version'
    return compose_file, targets, grace, label, health_url

def run(args, check=True):
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(args) + ': ' + (p.stderr or p.stdout)[-1200:])
    return p

def compose(compose_file, *args, check=True):
    return run(['docker', 'compose', '-f', str(compose_file), *args], check=check)

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_state(data):
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    temp = STATE_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2) + chr(10))
    os.chmod(temp, 0o600)
    os.replace(temp, STATE_FILE)

def credentials():
    if not TELEGRAM_FILE.is_file():
        raise RuntimeError('Telegram is not configured; refusing to update')
    values = {}
    for line in TELEGRAM_FILE.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    token, chat = values.get('BOT_TOKEN', ''), values.get('CHAT_ID', '')
    if not token or not chat:
        raise RuntimeError('Telegram configuration is incomplete; refusing to update')
    return token, chat

def telegram_request(token, method, fields=None):
    url = 'https://api.telegram.org/bot' + token + '/' + method
    data = urllib.parse.urlencode(fields or {}).encode() if fields is not None else None
    request = urllib.request.Request(url, data=data, method='POST' if data else 'GET')
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError('Telegram returned HTTP ' + str(e.code)) from None
    except Exception:
        raise RuntimeError('Telegram request failed') from None
    if not result.get('ok'):
        raise RuntimeError('Telegram rejected the request')
    return result

def notify(label, message):
    token, chat = credentials()
    telegram_request(token, 'sendMessage', {'chat_id': chat, 'text': f'{label} - {message}', 'disable_web_page_preview': 'true'})

def check_telegram():
    token, _ = credentials()
    telegram_request(token, 'getMe')

def running_info(compose_file, service):
    cid = compose(compose_file, 'ps', '-q', service, check=False).stdout.strip()
    if not cid:
        return None
    obj = json.loads(run(['docker', 'inspect', cid]).stdout)[0]
    return {'id': obj['Image'], 'status': obj['State']['Status']}

def healthy(compose_file, targets, health_url):
    for service in targets:
        info = running_info(compose_file, service)
        if not info or info['status'] != 'running':
            return False, service + ' is not running'
    request = urllib.request.Request(health_url, headers={'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            code = response.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        return False, 'NetBird API endpoint is unreachable'
    if code not in (200, 401):
        return False, 'NetBird API returned HTTP ' + str(code)
    return True, 'containers running; API reachable (HTTP ' + str(code) + ')'

def candidates(compose_file, targets):
    services = json.loads(compose(compose_file, 'config', '--format', 'json').stdout)['services']
    result = {}
    for service in targets:
        image = services[service].get('image')
        if not image:
            raise RuntimeError('No image configured for ' + service)
        image_id = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image]).stdout.strip()
        result[service] = {'image': image, 'id': image_id}
    return result

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

def main():
    compose_file, targets, grace, label, health_url = settings()
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    # Fail closed: no image pull/update unless Telegram credentials and API are available.
    check_telegram()
    ok, reason = healthy(compose_file, targets, health_url)
    if not ok:
        notify(label, 'CONTAINER update bloquée — précontrôle échoué: ' + reason)
        print('BLOCKED:', reason)
        return 2
    before = {service: running_info(compose_file, service) for service in targets}
    if any(not info or info['status'] != 'running' for info in before.values()):
        raise RuntimeError('A target NetBird service is not running')
    pull = compose(compose_file, 'pull', *targets, check=False)
    if pull.returncode:
        notify(label, 'CONTAINER update — vérification des images échouée')
        print((pull.stderr or pull.stdout)[-1200:])
        return 2
    found = candidates(compose_file, targets)
    if all(found[s]['id'] == before[s]['id'] for s in targets):
        state = load_state()
        if state.get('candidate') or state.get('blocked'):
            save_state({'last_check': time.time()})
        print('NO_UPDATE')
        return 0
    state = load_state()
    sig = fingerprint(found)
    if state.get('blocked') == sig:
        print('BLOCKED_AFTER_FAILED_UPDATE')
        return 2
    if state.get('candidate') != found:
        save_state({'candidate': found, 'first_seen': time.time()})
        notify(label, f'CONTAINER update détectée pour NetBird; délai de sécurité de {grace // 3600} h commencé. Aucun changement appliqué.')
        print('CANDIDATE_RECORDED')
        return 0
    age = time.time() - float(state.get('first_seen', time.time()))
    if age < grace:
        print('WAITING_GRACE_SECONDS', int(grace - age))
        return 0
    backup = run(['/usr/local/sbin/netbird-backup.py']).stdout.strip()
    update = compose(compose_file, 'up', '-d', '--no-deps', *targets, check=False)
    if update.returncode:
        state['blocked'] = sig
        state.pop('candidate', None)
        save_state(state)
        notify(label, 'CONTAINER update échouée. Sauvegarde conservée; vérifie les journaux systemd.')
        print((update.stderr or update.stdout)[-1200:])
        return 2
    deadline = time.time() + 180
    ok, reason = False, 'health check timed out'
    while time.time() < deadline:
        ok, reason = healthy(compose_file, targets, health_url)
        if ok:
            break
        time.sleep(5)
    if not ok:
        state['blocked'] = sig
        state.pop('candidate', None)
        save_state(state)
        notify(label, 'ALERTE — CONTAINER update appliquée mais contrôle échoué (' + reason + '). Sauvegarde: ' + backup)
        print('POST_UPDATE_FAILED:', reason)
        return 2
    save_state({'last_applied': found, 'last_applied_at': time.time()})
    notify(label, 'CONTAINER update terminée pour NetBird; services et API vérifiés. ' + backup)
    print('UPDATE_OK:', reason, backup)
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('ERROR:', str(error), file=sys.stderr)
        sys.exit(2)
