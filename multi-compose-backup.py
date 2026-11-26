#!/usr/bin/env python3
"""Back up configured containers and volumes while containers are stopped."""
import os
import json
import pathlib
import subprocess
import tarfile
import time

BACKUP_DIR = pathlib.Path('/var/backups/vps-update-apps')
CONFIG = pathlib.Path('/etc/vps-update-agent/projects.json')


def run(*args):
    return subprocess.run(args, text=True, capture_output=True, check=True, timeout=300).stdout.strip()


def main():
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    config = json.loads(CONFIG.read_text())
    running = []
    for name in config['containers']:
        state = run('docker', 'inspect', '--format', '{{.State.Running}}', name)
        if state != 'true':
            raise RuntimeError('Expected container is not running: ' + name)
        running.append(name)
    paths = []
    for volume in config['volumes']:
        path = pathlib.Path(run('docker', 'volume', 'inspect', '--format', '{{.Mountpoint}}', volume))
        if not path.is_dir() or not path.is_absolute():
            raise RuntimeError('Volume directory is inaccessible: ' + volume)
        paths.append((volume, path))
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    archive = BACKUP_DIR / ('apps-' + stamp + '.tar.gz')
    tmp = BACKUP_DIR / ('apps-' + stamp + '.partial')
    stopped = []
    try:
        for container in running:
            stopped.append(container)
            run('docker', 'stop', '--time', '30', container)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            with tarfile.open(fileobj=stream, mode='w:gz', compresslevel=1) as tar:
                for volume, path in paths:
                    tar.add(path, arcname='volumes/' + volume)
                for config_dir in config['config_dirs']:
                    path = pathlib.Path(config_dir)
                    if not path.is_dir():
                        raise RuntimeError('Configuration directory missing: ' + str(path))
                    tar.add(path, arcname='config/' + path.name)
        with tarfile.open(tmp, 'r:gz') as tar:
            for item in tar:
                if item.isfile():
                    f = tar.extractfile(item)
                    while f.read(1024 * 1024):
                        pass
        os.replace(tmp, archive)
    finally:
        failures = []
        for container in reversed(stopped):
            try:
                run('docker', 'start', container)
            except Exception:
                failures.append(container)
        tmp.unlink(missing_ok=True)
        if failures:
            raise RuntimeError('Failed to restart containers: ' + ', '.join(failures))
    now = time.time()
    archives = sorted(BACKUP_DIR.glob('apps-*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in archives[1:]:
        if now - old.stat().st_mtime > 86400:
            old.unlink()
    print(str(archive))

if __name__ == '__main__':
    main()
