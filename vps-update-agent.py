#!/usr/bin/env python3
"""Approval-based Ubuntu and Docker Compose update agent for Telegram."""
import fcntl
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG = pathlib.Path('/etc/vps-update-agent/config.env')
SECRETS = pathlib.Path('/etc/vps-update-agent/telegram.env')
STATE_DIR = pathlib.Path('/var/lib/vps-update-agent')
STATE = STATE_DIR / 'state.json'
LOCK = STATE_DIR / 'operation.lock'
REBOOT_MARKER = STATE_DIR / 'reboot-pending.json'
LOG = pathlib.Path('/var/log/vps-update-agent.log')


def read_env(path):
    values = {}
    if not path.is_file():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def settings():
    c = read_env(CONFIG)
    compose_file = pathlib.Path(c.get('COMPOSE_FILE', '/root/docker-compose.yml'))
    health_url = c.get('HEALTHCHECK_URL', '').strip()
    if not health_url:
        proxy_env = compose_file.parent / 'proxy.env'
        for line in read_env(proxy_env).items():
            if line[0] == 'NB_PROXY_DOMAIN':
                domain = line[1].rstrip('/')
                health_url = domain + '/api/instance/version'
                if not health_url.startswith(('http://', 'https://')):
                    health_url = 'https://' + health_url
                break
    return {
        'label': c.get('VPS_LABEL', 'VPS').strip() or 'VPS',
        'compose': compose_file,
        'grace': max(1, int(c.get('GRACE_HOURS', '24'))) * 3600,
        'prefix': c.get('MESSAGE_PREFIX', 'UPDATE').strip() or 'UPDATE',
        'health_url': health_url,
    }


def credentials():
    c = read_env(SECRETS)
    token, chat, user = c.get('BOT_TOKEN', ''), c.get('CHAT_ID', ''), c.get('USER_ID', '')
    if not token or not chat or not user:
        raise RuntimeError('Telegram credentials are missing or incomplete')
    return token, str(chat), str(user)


def api(method, fields=None, timeout=20):
    token, _, _ = credentials()
    url = 'https://api.telegram.org/bot' + token + '/' + method
    body = json.dumps(fields).encode() if fields is not None else None
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'}, method='POST' if body else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            result = json.loads(res.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError('Telegram HTTP ' + str(exc.code)) from None
    except Exception as exc:
        raise RuntimeError('Telegram request failed: ' + type(exc).__name__) from None
    if not result.get('ok'):
        raise RuntimeError('Telegram rejected the request')
    return result.get('result')


def send(text, keyboard=None):
    _, chat, _ = credentials()
    fields = {'chat_id': chat, 'text': text, 'disable_web_page_preview': True}
    if keyboard:
        fields['reply_markup'] = {'inline_keyboard': keyboard}
    return api('sendMessage', fields)


def answer_callback(callback_id, text='Reçu'):
    try:
        api('answerCallbackQuery', {'callback_query_id': callback_id, 'text': text})
    except Exception:
        pass


def edit_message(chat, message_id, text):
    try:
        api('editMessageText', {'chat_id': chat, 'message_id': message_id, 'text': text})
    except Exception:
        pass


def run(args, check=True, log=False, timeout=None):
    env = os.environ.copy()
    if args and args[0] in ('apt', 'apt-get'):
        env['DEBIAN_FRONTEND'] = 'noninteractive'
    if log:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 2 * 1024 * 1024:
            rotated = LOG.with_suffix('.log.1')
            rotated.unlink(missing_ok=True)
            LOG.replace(rotated)
        with LOG.open('a') as stream:
            os.chmod(LOG, 0o600)
            stream.write('\n[' + time.strftime('%Y-%m-%d %H:%M:%S %z') + '] $ ' + ' '.join(args) + '\n')
            stream.flush()
            p = subprocess.run(args, text=True, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout, env=env)
        return p
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    if check and p.returncode:
        detail = (p.stderr or p.stdout)[-1000:]
        raise RuntimeError('Command failed (' + ' '.join(args[:3]) + '): ' + detail)
    return p


def compose(cfg, *args, check=True, log=False):
    if not cfg['compose'].is_file():
        raise RuntimeError('Compose file not found: ' + str(cfg['compose']))
    return run(['docker', 'compose', '-f', str(cfg['compose']), *args], check=check, log=log)


def state_read():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def state_write(value):
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


def locked():
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    handle = LOCK.open('a')
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def save_offset(offset):
    with locked():
        state = state_read()
        state['telegram_offset'] = offset
        state_write(state)


def image_candidates(cfg):
    services = json.loads(compose(cfg, 'config', '--format', 'json').stdout)['services']
    running = {}
    for service in services:
        ids = compose(cfg, 'ps', '-q', service, check=False).stdout.splitlines()
        if ids:
            running[service] = ids
    images = [s for s, definition in services.items() if definition.get('image') and s in running]
    if images:
        compose(cfg, 'pull', *images, log=True, timeout=1800)
    changes = {}
    for service in images:
        image = services[service]['image']
        wanted = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image]).stdout.strip()
        current_ids = set()
        for cid in running[service]:
            inspected = json.loads(run(['docker', 'inspect', cid]).stdout)[0]
            current_ids.add(inspected['Image'])
        if wanted not in current_ids:
            changes[service] = {'image': image, 'current': sorted(current_ids), 'available': wanted}
    return changes


def scan_candidate(cfg):
    run(['apt-get', 'update', '-qq'], log=True, timeout=1800)
    apt = run(['apt', 'list', '--upgradable'], check=False)
    packages = [line.split('/', 1)[0] + ' ' + line.split()[1]
                for line in apt.stdout.splitlines()
                if '/' in line and len(line.split()) > 1 and not line.startswith('Listing')]
    docker = image_candidates(cfg)
    candidate = {'packages': sorted(set(packages)), 'docker': docker}
    return candidate if packages or docker else None


def digest(candidate):
    return hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()


def notify_candidate(cfg, candidate, signature, stable=False):
    count_apt, count_docker = len(candidate['packages']), len(candidate['docker'])
    summary = f"{count_apt} paquet(s) Ubuntu, {count_docker} service(s) Docker"
    if not stable:
        send(f"{cfg['label']} - {cfg['prefix']} détectée: {summary}. Délai de stabilité de {cfg['grace'] // 3600} h en cours; je te demanderai ton accord ensuite.")
        return
    lines = [f"{cfg['label']} - {cfg['prefix']} prête après le délai de stabilité.", summary,
             'Installer mettra à jour Ubuntu et les services Docker du fichier Compose. Le VPS redémarrera seulement si Ubuntu le demande.']
    if candidate['packages']:
        lines.append('Paquets: ' + ', '.join(candidate['packages'][:20]))
    if candidate['docker']:
        lines.append('Services Docker: ' + ', '.join(sorted(candidate['docker'])[:20]))
    short = signature[:32]
    send('\n'.join(lines), [[
        {'text': '✅ Installer et redémarrer si nécessaire', 'callback_data': 'apply:' + short},
        {'text': 'Ignorer', 'callback_data': 'skip:' + short},
    ]])


def scan():
    cfg = settings()
    with locked():
        state = state_read()
        try:
            candidate = scan_candidate(cfg)
        except Exception as exc:
            try:
                send(f"{cfg['label']} - ALERTE {cfg['prefix']}: vérification impossible; rien n'a été installé. Consulte journalctl -u vps-update-scan.service. Détail: {str(exc)[:600]}")
            except Exception:
                pass
            raise
        if candidate is None:
            if state.get('pending'):
                state.pop('pending', None)
                state.pop('declined', None)
                state_write(state)
                send(f"{cfg['label']} - {cfg['prefix']}: tout est à jour; les mises à jour en attente ne sont plus disponibles.")
            print('UP_TO_DATE')
            return
        sig = digest(candidate)
        if state.get('declined') == sig:
            print('IGNORED_PREVIOUSLY')
            return
        pending = state.get('pending')
        if not pending or pending.get('signature') != sig:
            pending = {'signature': sig, 'candidate': candidate, 'first_seen': time.time(), 'status': 'waiting', 'detected_notified': False}
            state['pending'] = pending
            state_write(state)
        if pending['status'] in ('applying', 'failed'):
            print('PENDING_' + pending['status'].upper())
            return
        age = time.time() - float(pending['first_seen'])
        if age < cfg['grace']:
            if not pending.get('detected_notified'):
                notify_candidate(cfg, candidate, sig, stable=False)
                pending['detected_notified'] = True
                state_write(state)
            print('WAITING_STABILITY', int(cfg['grace'] - age))
            return
        if pending['status'] != 'ready':
            notify_candidate(cfg, candidate, sig, stable=True)
            pending['status'] = 'ready'
            pending['prompted_at'] = time.time()
            state_write(state)
        print('AWAITING_TELEGRAM_APPROVAL')


def authorized_callback(update):
    _, chat_id, user_id = credentials()
    q = update['callback_query']
    message = q.get('message') or {}
    return (str(q.get('from', {}).get('id')) == user_id
            and str(message.get('chat', {}).get('id')) == chat_id)


def find_running_services(cfg):
    services = json.loads(compose(cfg, 'config', '--format', 'json').stdout)['services']
    result = []
    for service in services:
        ids = compose(cfg, 'ps', '-q', service, check=False).stdout.splitlines()
        if ids:
            result.append(service)
    return result


def check_netbird_api(cfg):
    url = cfg['health_url']
    if not url:
        raise RuntimeError('NetBird API URL missing; set HEALTHCHECK_URL in /etc/vps-update-agent/config.env')
    if not url.endswith('/api/instance/version'):
        url = url.rstrip('/') + '/api/instance/version'
    request = urllib.request.Request(url, headers={'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception as exc:
        raise RuntimeError('NetBird API inaccessible (' + type(exc).__name__ + ')') from None
    if status not in (200, 401):
        raise RuntimeError('NetBird API returned HTTP ' + str(status))
    return status


def create_system_backup():
    backup_dir = pathlib.Path('/var/backups/vps-update')
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    archive = backup_dir / f'system-{stamp}.tar.gz'
    stage = pathlib.Path(tempfile.mkdtemp(prefix='vps-update-backup-'))
    try:
        (stage / 'dpkg-selections.txt').write_text(run(['dpkg', '--get-selections']).stdout)
        (stage / 'apt-manual-packages.txt').write_text(run(['apt-mark', 'showmanual']).stdout)
        with tarfile.open(archive, 'w:gz', compresslevel=1) as tar:
            etc_dir = pathlib.Path('/etc')
            if etc_dir.exists():
                tar.add(etc_dir, arcname='etc', recursive=True)
            dpkg_status = pathlib.Path('/var/lib/dpkg/status')
            if dpkg_status.exists():
                tar.add(dpkg_status, arcname='var/lib/dpkg/status')
            for item in stage.iterdir():
                tar.add(item, arcname=item.name)
        os.chmod(archive, 0o600)
        os.chown(archive, 0, 0)
        now = time.time()
        archives = sorted(backup_dir.glob('system-*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in archives[1:]:
            if now - old.stat().st_mtime > 24 * 3600:
                old.unlink(missing_ok=True)
        return str(archive)
    finally:
        for item in stage.iterdir():
            item.unlink(missing_ok=True)
        stage.rmdir()


def apply_approved(cfg, signature):
    with locked():
        state = state_read()
        pending = state.get('pending') or {}
        if pending.get('status') != 'ready' or not pending.get('signature', '').startswith(signature):
            return 'Cette approbation a expiré. Je vais revérifier les mises à jour.'
        pending['status'] = 'applying'
        state_write(state)
        try:
            fresh = scan_candidate(cfg)
            if fresh is None or digest(fresh) != pending['signature']:
                state['pending'] = None
                state_write(state)
                send(f"{cfg['label']} - {cfg['prefix']}: les mises à jour ont changé; le délai de stabilité repart.")
                # A later scheduled scan records the refreshed candidate.
                return 'La liste des mises à jour a changé; délai de stabilité relancé.'

            previously_running = find_running_services(cfg)
            if fresh['docker']:
                check_netbird_api(cfg)
            system_backup = create_system_backup()
            run(['apt-get', '-y', '-o', 'Dpkg::Options::=--force-confdef',
                 '-o', 'Dpkg::Options::=--force-confold', 'upgrade'], log=True, timeout=14400)

            backup = None
            if fresh['docker']:
                backup_run = run(['/usr/local/sbin/vps-update-backup.py'], check=False, timeout=3600)
                if backup_run.returncode:
                    raise RuntimeError('Sauvegarde pré-Docker échouée: ' + (backup_run.stderr or backup_run.stdout)[-800:])
                backup = backup_run.stdout.strip()
            if previously_running:
                compose(cfg, 'up', '-d', *previously_running, log=True, timeout=3600)
                for service in previously_running:
                    ids = compose(cfg, 'ps', '-q', service, check=False).stdout.splitlines()
                    if not ids:
                        raise RuntimeError('Service Docker absent après mise à jour: ' + service)
                    for cid in ids:
                        status = run(['docker', 'inspect', '--format', '{{.State.Status}}', cid]).stdout.strip()
                        if status != 'running':
                            raise RuntimeError('Service Docker non actif après mise à jour: ' + service)
            if fresh['docker']:
                deadline = time.time() + 180
                last_error = 'API health check timed out'
                while time.time() < deadline:
                    try:
                        api_status = check_netbird_api(cfg)
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        time.sleep(5)
                else:
                    raise RuntimeError('NetBird API après mise à jour: ' + last_error)

            state = state_read()
            state.pop('pending', None)
            state.pop('declined', None)
            state['last_success_at'] = time.time()
            state['last_success_signature'] = signature
            state_write(state)
            detail = f"{len(fresh['packages'])} paquet(s) Ubuntu et {len(fresh['docker'])} service(s) Docker vérifiés."
            detail += ' Sauvegarde système: ' + system_backup + '.'
            if backup:
                detail += ' Sauvegarde NetBird: ' + backup
            if pathlib.Path('/var/run/reboot-required').exists():
                REBOOT_MARKER.write_text(json.dumps({'message': detail, 'started_at': time.time()}))
                os.chmod(REBOOT_MARKER, 0o600)
                try:
                    send(f"{cfg['label']} - {cfg['prefix']}: mises à jour terminées. Redémarrage Ubuntu requis; le VPS redémarre maintenant.")
                except Exception:
                    REBOOT_MARKER.unlink(missing_ok=True)
                    raise
                reboot = run(['systemctl', 'reboot'], check=False)
                if reboot.returncode:
                    REBOOT_MARKER.unlink(missing_ok=True)
                    raise RuntimeError('systemctl reboot failed')
                return 'Redémarrage demandé.'
            send(f"{cfg['label']} - {cfg['prefix']} terminée. {detail}")
            return 'Mises à jour terminées; redémarrage non requis.'
        except Exception as exc:
            state = state_read()
            if state.get('pending'):
                state['pending']['status'] = 'failed'
                state_write(state)
            send(f"{cfg['label']} - ALERTE {cfg['prefix']}: échec; aucune nouvelle tentative automatique pour cette même version. Consulte journalctl -u vps-update-agent.service. Détail: {str(exc)[:700]}")
            raise


def handle_update(update, already_answered=False):
    q = update['callback_query']
    if not authorized_callback(update):
        if not already_answered:
            answer_callback(q['id'], 'Utilisateur non autorisé')
        return
    if not already_answered:
        answer_callback(q['id'])
    data = q.get('data', '')
    action, _, signature = data.partition(':')
    if action not in ('apply', 'skip') or len(signature) != 32:
        return
    cfg = settings()
    chat = str(q['message']['chat']['id'])
    message_id = q['message']['message_id']
    if action == 'skip':
        with locked():
            state = state_read()
            pending = state.get('pending') or {}
            if pending.get('status') != 'ready' or not pending.get('signature', '').startswith(signature):
                edit_message(chat, message_id, f"{cfg['label']} - demande expirée; aucun changement appliqué.")
                return
            state['declined'] = pending['signature']
            state.pop('pending', None)
            state_write(state)
        edit_message(chat, message_id, f"{cfg['label']} - mises à jour ignorées. Rien n'a été installé.")
        return
    edit_message(chat, message_id, f"{cfg['label']} - approbation reçue; vérification puis installation des mises à jour...")
    apply_approved(cfg, signature)


def send_reboot_completion():
    if not REBOOT_MARKER.exists():
        return
    cfg = settings()
    marker = json.loads(REBOOT_MARKER.read_text())
    send(f"{cfg['label']} - redémarrage terminé; le VPS est de nouveau en ligne. {marker.get('message', '')}")
    REBOOT_MARKER.unlink(missing_ok=True)


def bot_loop():
    try:
        send_reboot_completion()
    except Exception:
        pass
    interrupted = False
    with locked():
        state = state_read()
        pending = state.get('pending') or {}
        if pending.get('status') == 'applying' and not REBOOT_MARKER.exists():
            pending['status'] = 'failed'
            state_write(state)
            interrupted = True
    if interrupted:
        try:
            cfg = settings()
            send(f"{cfg['label']} - ALERTE UPDATE: l'installation a été interrompue avant sa fin; aucune reprise automatique. Consulte les journaux systemd avant de relancer.")
        except Exception:
            pass
    state = state_read()
    offset = int(state.get('telegram_offset', 0))
    while True:
        if REBOOT_MARKER.exists():
            try:
                send_reboot_completion()
            except Exception:
                pass
        try:
            updates = api('getUpdates', {'offset': offset, 'timeout': 25, 'allowed_updates': ['callback_query']}, timeout=35) or []
            for update in updates:
                offset = max(offset, int(update['update_id']) + 1)
                if 'callback_query' in update:
                    q = update['callback_query']
                    if authorized_callback(update):
                        answer_callback(q['id'])
                        save_offset(offset)
                        handle_update(update, already_answered=True)
                    else:
                        answer_callback(q['id'], 'Utilisateur non autorisé')
                        save_offset(offset)
                else:
                    save_offset(offset)
        except Exception as exc:
            print('BOT_ERROR:', str(exc), file=sys.stderr, flush=True)
            time.sleep(10)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root')
    if '--scan' in sys.argv:
        scan()
    elif '--bot' in sys.argv:
        bot_loop()
    else:
        raise SystemExit('Usage: vps-update-agent.py --scan|--bot')


if __name__ == '__main__':
    main()
