#!/usr/bin/env python3
"""Adapter for multiple independent Compose projects."""
import importlib.util
import json
import pathlib
import time

spec = importlib.util.spec_from_file_location('vps_update_base', '/usr/local/sbin/vps-update-agent.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
def projects():
    data = json.loads(pathlib.Path('/etc/vps-update-agent/projects.json').read_text())
    return [(item['file'], item['name']) for item in data['projects']]


def project_cfg(cfg, path):
    return dict(cfg, compose=pathlib.Path(path))


def image_candidates(cfg):
    result = {}
    for path, name in projects():
        for service, details in base_image_candidates(project_cfg(cfg, path)).items():
            result[name + '/' + service] = details
    return result


def running_by_project(cfg):
    return [(project_cfg(cfg, path), name, base.find_running_services(project_cfg(cfg, path)))
            for path, name in projects()]


def apply_approved(cfg, signature):
    with base.locked():
        state = base.state_read()
        pending = state.get('pending') or {}
        if pending.get('status') != 'ready' or not pending.get('signature', '').startswith(signature):
            return 'Cette approbation a expiré.'
        pending['status'] = 'applying'
        base.state_write(state)
        try:
            fresh = base.scan_candidate(cfg)
            if fresh is None or base.digest(fresh) != pending['signature']:
                state['pending'] = None
                base.state_write(state)
                base.send(f"{cfg['label']} - UPDATE : liste modifiée; nouveau délai de stabilité.")
                return 'Liste modifiée; délai relancé.'
            running = running_by_project(cfg)
            system_backup = base.create_system_backup()
            backup = None
            if fresh['docker']:
                result = base.run(['/usr/local/sbin/multi-compose-backup.py'], timeout=3600)
                backup = result.stdout.strip()
            base.run(['apt-get', '-y', '-o', 'Dpkg::Options::=--force-confdef', '-o',
                      'Dpkg::Options::=--force-confold', 'full-upgrade'], log=True, timeout=14400)
            for project, name, services in running:
                changed = [s for s in services if name + '/' + s in fresh['docker']]
                if changed:
                    base.compose(project, 'up', '-d', '--no-deps', *changed, log=True, timeout=3600)
                for service in services:
                    ids = base.compose(project, 'ps', '-q', service).stdout.splitlines()
                    if not ids:
                        raise RuntimeError('Service absent: ' + name + '/' + service)
                    for cid in ids:
                        status = base.run(['docker', 'inspect', '--format', '{{.State.Status}}', cid]).stdout.strip()
                        if status != 'running':
                            raise RuntimeError('Service non actif: ' + name + '/' + service)
            state = base.state_read()
            state.pop('pending', None)
            state.pop('declined', None)
            state['last_success_at'] = time.time()
            state['last_success_signature'] = signature
            state['last_success_detail'] = f"{len(fresh['apt_plan'])} action(s) APT; {len(fresh['docker'])} service(s) Docker."
            base.state_write(state)
            detail = f"{len(fresh['apt_plan'])} action(s) APT; {len(fresh['docker'])} service(s) Docker. Sauvegarde système: {system_backup}."
            if backup:
                detail += ' Sauvegarde applications: ' + backup
            if pathlib.Path('/var/run/reboot-required').exists():
                import json, os
                base.REBOOT_MARKER.write_text(json.dumps({'message': detail, 'started_at': time.time()}))
                os.chmod(base.REBOOT_MARKER, 0o600)
                try:
                    base.send(f"{cfg['label']} - UPDATE : mises à jour terminées; redémarrage requis en cours.")
                    base.run(['systemctl', 'reboot'])
                except Exception:
                    base.REBOOT_MARKER.unlink(missing_ok=True)
                    raise
                return 'Redémarrage demandé.'
            base.send(f"{cfg['label']} - UPDATE terminée. {detail}")
            return 'Mises à jour terminées.'
        except Exception as exc:
            state = base.state_read()
            if state.get('pending'):
                state['pending']['status'] = 'failed'
                base.state_write(state)
            base.send(f"{cfg['label']} - ALERTE UPDATE : échec; aucune répétition automatique. {str(exc)[:600]}")
            raise


base_image_candidates = base.image_candidates
base.image_candidates = image_candidates
base.apply_approved = apply_approved
if __name__ == '__main__':
    base.main()
