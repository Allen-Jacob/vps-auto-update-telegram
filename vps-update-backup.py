#!/usr/bin/env python3
"""Create a consistent, root-only NetBird pre-update backup."""
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
import time

CONFIG_FILE = pathlib.Path('/etc/vps-update-agent/config.env')

def settings():
    values = dict(os.environ)
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    compose_file = pathlib.Path(values.get('COMPOSE_FILE', '/root/docker-compose.yml'))
    return compose_file, pathlib.Path(values.get('BACKUP_DIR', '/var/backups/netbird')), int(values.get('RETENTION_HOURS', '24'))

def run(args, check=True):
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(args) + ': ' + (p.stderr or p.stdout)[-1200:])
    return p.stdout.strip()

def compose(compose_file, *args):
    return run(['docker', 'compose', '-f', str(compose_file), *args])

def container_id(compose_file, service):
    return run(['docker', 'compose', '-f', str(compose_file), 'ps', '-a', '-q', service], check=False)

def mount_source(compose_file, service, destination):
    cid = container_id(compose_file, service)
    if not cid:
        raise RuntimeError('No Compose container found for ' + service)
    mounts = json.loads(run(['docker', 'inspect', '--format', '{{json .Mounts}}', cid]))
    for mount in mounts:
        if mount.get('Destination') == destination:
            return pathlib.Path(mount['Source'])
    raise RuntimeError('Missing mount ' + destination + ' on ' + service)

def secure_tree(path):
    for base, _, files in os.walk(path):
        os.chmod(base, 0o700)
        for name in files:
            item = pathlib.Path(base) / name
            if not item.is_symlink():
                os.chmod(item, 0o600)

def main():
    compose_file, out_dir, retention_hours = settings()
    root = compose_file.parent
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(out_dir, 0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    archive = out_dir / f'netbird-{stamp}.tar.gz'
    stage = pathlib.Path(tempfile.mkdtemp(prefix='netbird-backup-'))
    services = json.loads(compose(compose_file, 'config', '--format', 'json'))['services']
    stopped = []
    try:
        for service in ('netbird-server', 'crowdsec'):
            if service not in services:
                continue
            cid = container_id(compose_file, service)
            if cid and run(['docker', 'inspect', '--format', '{{.State.Status}}', cid]) == 'running':
                stopped.append(service)
        if stopped:
            compose(compose_file, 'stop', *stopped)
        try:
            compose_copy = stage / compose_file.name
            shutil.copy2(compose_file, compose_copy)
            for name in ('docker-compose.yml', 'compose.yml', 'compose.yaml', 'config.yaml', 'dashboard.env', 'proxy.env', 'traefik-dynamic.yaml', '.env'):
                source = root / name
                if source.is_file():
                    shutil.copy2(source, stage / name)
            crowdsec_config = root / 'crowdsec'
            if crowdsec_config.is_dir():
                shutil.copytree(crowdsec_config, stage / 'crowdsec', symlinks=True)
            shutil.copytree(mount_source(compose_file, 'netbird-server', '/var/lib/netbird'), stage / 'volumes/netbird_data', symlinks=True)
            if 'crowdsec' in services:
                try:
                    shutil.copytree(mount_source(compose_file, 'crowdsec', '/var/lib/crowdsec/data'), stage / 'volumes/crowdsec_db', symlinks=True)
                except RuntimeError:
                    pass
            (stage / 'BACKUP-INFO.txt').write_text('NetBird pre-update backup. Contains deployment secrets and database state. Store securely.\n')
        finally:
            if stopped:
                compose(compose_file, 'start', *stopped)
        secure_tree(stage)
        with tarfile.open(archive, 'w:gz', compresslevel=1) as tar:
            tar.add(stage, arcname='.')
        os.chmod(archive, 0o600)
        os.chown(archive, 0, 0)
        shutil.rmtree(stage, ignore_errors=True)
        now = time.time()
        backups = sorted(out_dir.glob('netbird-*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[1:]:
            if now - old.stat().st_mtime > retention_hours * 3600:
                old.unlink(missing_ok=True)
        print(f'BACKUP_OK {archive} bytes={archive.stat().st_size}')
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

if __name__ == '__main__':
    main()
