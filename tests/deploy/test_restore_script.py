"""deploy/restore.sh with a stand-in for docker: which commands it runs, in which order, and what it does when
a step fails. tools/deploy-drill.sh runs the real thing (CI job "deploy drill")."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform == 'win32' or shutil.which('bash') is None, reason='bash script')

DOCKER = '''#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
case "$*" in *pg_restore*) cat > /dev/null;; esac
for f in $FAKE_FAIL; do
  case "$*" in *"$f"*) echo "fake docker: $f fails" >&2; exit 1;; esac
done
'''


def restore(tmp_path, fail=''):
    deploy = tmp_path / 'deploy'
    deploy.mkdir()
    shutil.copy(REPO / 'deploy' / 'restore.sh', deploy)
    (deploy / '.env').write_text('FW_DB_PASSWORD=x\nFW_STORE_DIR=/nowhere\n')
    backup = tmp_path / 'backup'
    (backup / 'db').mkdir(parents=True)
    (backup / 'store' / 'objects').mkdir(parents=True)
    (backup / 'db' / 'fw-1.dump').write_bytes(b'dump')
    (backup / 'LATEST').write_text('fw-1.dump\n')
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    for name, body in (('docker', DOCKER), ('sleep', '#!/bin/sh\n')):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    log = tmp_path / 'docker.log'
    env = dict(os.environ, PATH=f'{bin_}:{os.environ["PATH"]}', FAKE_LOG=str(log), FAKE_FAIL=fail)
    r = subprocess.run(['bash', str(deploy / 'restore.sh'), str(backup)], env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    return r, log.read_text().splitlines(), backup


def steps(lines):
    """The log reduced to the steps that matter (repeated health checks count once)."""
    names = [('compose stop server', 'stop'), ('RENAME TO fw_before_restore_', 'rename fw away'),
             ('CREATE DATABASE fw', 'create'), ('pg_restore', 'pg_restore'),
             ('verify --rehash --repair-from /backup', 'verify'), ('DROP DATABASE IF EXISTS fw', 'drop restored'),
             ('compose start server', 'start'), ('compose restart server', 'restart'), ('/api/v1/health', 'health')]
    out = []
    for line in lines:
        name = 'rename back' if line.endswith('RENAME TO fw') else next((n for k, n in names if k in line), None)
        if name and not (out and out[-1] == name == 'health'):
            out.append(name)
    return out


def test_success_verifies_before_serving(tmp_path):
    r, lines, backup = restore(tmp_path)
    assert r.returncode == 0, r.stdout
    assert steps(lines) == ['stop', 'rename fw away', 'create', 'pg_restore', 'verify', 'start', 'health']
    run = next(line for line in lines if 'compose run' in line)
    assert f'-v {backup}/store:/backup:ro' in run and '--service-ports' not in run and 'fw-server migrate' in run
    assert '恢复完成' in r.stdout


@pytest.mark.parametrize('fail', ['verify', 'pg_restore'])
def test_failed_check_or_load_swaps_the_original_database_back(tmp_path, fail):
    r, lines, _ = restore(tmp_path, fail)
    assert r.returncode == 1, r.stdout
    done = steps(lines)
    assert done[:4] == ['stop', 'rename fw away', 'create', 'pg_restore']
    assert done[-4:] == ['drop restored', 'rename back', 'restart', 'health']
    assert 'start' not in done  # never served the restored database
    assert '换回原来的数据库' in r.stdout and '恢复完成' not in r.stdout


def test_service_that_does_not_come_up_on_the_restored_database_is_rolled_back(tmp_path):
    r, lines, _ = restore(tmp_path, '/api/v1/health')
    assert r.returncode == 1, r.stdout
    assert steps(lines)[-6:] == ['start', 'health', 'drop restored', 'rename back', 'restart', 'health']
    assert '换回原来的数据库' in r.stdout and '平台服务也没有起来' in r.stdout


def test_failure_before_the_swap_keeps_the_database(tmp_path):
    r, lines, _ = restore(tmp_path, 'RENAME TO fw_before_restore_')
    assert r.returncode == 1, r.stdout
    done = steps(lines)
    assert 'drop restored' not in done and done[-2:] == ['restart', 'health']
