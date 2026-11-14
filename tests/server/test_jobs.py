"""Job queue, leases, runners and fw worker (step 3, part 1)."""
import argparse
import hashlib
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest

from fw import cli as fw_cli
from fw import worker
from fw.api import Api, ApiError
from fwserver import auth, db

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='the worker runs on the Linux GPU machine')


def quiet(_):
    pass


PIP = 'numpy==1.26.4\npip==24.0\n'


@pytest.fixture
def fake_conda(tmp_path, monkeypatch):
    """A conda whose `list -n <env> --explicit` prints the file listings/<env>.txt and whose
    `run -n <env> python -m pip freeze` prints listings/<env>.pip (PIP when there is none). With listings/<env>.hang
    the listing hangs: conda starts a child, writes both process ids to listings/<env>.pids and waits."""
    listings = tmp_path / 'listings'
    listings.mkdir()
    script = tmp_path / 'conda'
    script.write_text('#!/bin/sh\n'
                      f'd="{listings}"\n'
                      'if [ "$1" = run ] && [ "$2" = -n ] && [ "$4" = python ]; then\n'
                      '  if [ -f "$d/$3.pip" ]; then cat "$d/$3.pip"; else printf "%s" "$FAKE_PIP"; fi; exit 0\n'
                      'fi\n'
                      '[ "$1" = list ] && [ "$2" = -n ] && [ "$4" = --explicit ] || exit 2\n'
                      'f="$d/$3.txt"\n'
                      '[ -f "$f" ] || { echo "EnvironmentLocationNotFound: $3" >&2; exit 1; }\n'
                      'if [ -f "$d/$3.hang" ]; then sleep 61 & echo "$$ $!" > "$d/$3.pids"; wait; fi\n'
                      'cat "$f"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv('FW_CONDA', str(script))
    monkeypatch.setenv('FAKE_PIP', PIP)

    def set_env(env, text, pip=None):
        (listings / f'{env}.txt').write_text(text)
        if pip is not None:
            (listings / f'{env}.pip').write_text(pip)
    set_env.listings = listings
    return set_env


def listing(conda_text, pip=PIP) -> bytes:
    """What a runner records: the conda listing, then pip freeze."""
    return conda_text.encode() + worker.PIP_HEADER + pip.encode()


@pytest.fixture
def make_worker(service, database):
    """A new worker account (its own machine) for one test, as an Api."""
    made = []

    def make(svc=None):
        name = f'gpu-{uuid.uuid4().hex[:6]}'
        with db.connect(database) as c:
            auth.add_user(c, name, 'worker')
            token = auth.create_token(c, name, 'test')[0]
        a = Api((svc or service).url, token)
        a.name = name
        made.append(a)
        return a

    yield make
    for a in made:
        a.close()


def running(command: str) -> bool:
    """Is a process with exactly this command line running (zombies aside)? Reads /proc, no pgrep needed."""
    for d in Path('/proc').iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = (d / 'cmdline').read_bytes().split(b'\0')
            state = (d / 'stat').read_text().rsplit(') ', 1)[1][0]
        except (OSError, IndexError):
            continue
        if ' '.join(a.decode('utf-8', 'replace') for a in argv if a) == command and state != 'Z':
            return True
    return False


def runner(api, fake_conda, name=None, listing='@EXPLICIT\nhttps://x/python-3.10.tar.bz2\n', src=''):
    name = name or f'r-{uuid.uuid4().hex[:6]}'
    fake_conda(name, listing)
    return worker.register_runner(api, name, name, src)


def git_repo(path):
    path.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    for args in (['init', '-q'],
                 ['-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '--allow-empty', '-m', 'a']):
        subprocess.run(['git', '-C', str(path), *args], check=True, env=env)
    (path / 'f.py').write_text('x = 1\n')
    subprocess.run(['git', '-C', str(path), 'add', 'f.py'], check=True, env=env)
    subprocess.run(['git', '-C', str(path), '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '-m', 'b'],
                   check=True, env=env)
    return path


def test_roles(make_api, make_worker, fake_conda):
    w = make_worker()
    r = runner(w, fake_conda)
    algo, field, deploy = make_api('algo'), make_api('field'), make_api('deploy')
    for a in (algo, make_api('admin')):
        with pytest.raises(ApiError) as e:
            a.claim()
        assert e.value.status == 403
    with pytest.raises(ApiError) as e:
        worker.register_runner(algo, 'x', r['conda_env'])
    assert e.value.status == 403
    with pytest.raises(ApiError) as e:
        w.submit_job('check', runner=r['runner'])
    assert e.value.status == 403  # a worker runs jobs, it does not submit them
    for a in (field, deploy):
        with pytest.raises(ApiError) as e:
            a.jobs()
        assert e.value.status == 403
    assert any(x['ref'] == r['ref'] for x in w.runners()) and any(x['ref'] == r['ref'] for x in algo.runners())


def test_register_runner_versions(make_worker, fake_conda, tmp_path):
    w = make_worker()
    name = f'yolo-{uuid.uuid4().hex[:6]}'
    fake_conda('yolo26', '@EXPLICIT\nhttps://x/torch-2.4.tar.bz2\n')
    src = git_repo(tmp_path / 'src')
    r1 = worker.register_runner(w, name, 'yolo26', str(src))
    assert r1['created'] and r1['ref'] == f'{name}@1' and r1['worker'] == w.name
    assert r1['env_sha256'] == hashlib.sha256(listing('@EXPLICIT\nhttps://x/torch-2.4.tar.bz2\n')).hexdigest()
    assert len(r1['src_commit']) == 40 and not r1['src_dirty'] and r1['src_dir'] == str(src.resolve())
    assert w.read_object(r1['env_sha256']) == listing('@EXPLICIT\nhttps://x/torch-2.4.tar.bz2\n')  # kept
    again = worker.register_runner(w, name, 'yolo26', str(src))
    assert not again['created'] and again['ref'] == r1['ref']
    (src / 'f.py').write_text('x = 2\n')  # a change not committed
    r2 = worker.register_runner(w, name, 'yolo26', str(src))
    assert r2['ref'] == f'{name}@2' and r2['src_dirty']
    fake_conda('yolo26', '@EXPLICIT\nhttps://x/torch-2.5.tar.bz2\n')
    r3 = worker.register_runner(w, name, 'yolo26', str(src))
    assert r3['ref'] == f'{name}@3' and r3['env_sha256'] != r2['env_sha256']
    other = make_worker()  # the same environment set up on another GPU machine
    r4 = worker.register_runner(other, name, 'yolo26', str(src))
    assert r4['ref'] == f'{name}@4' and r4['worker'] == other.name
    assert [v['number'] for v in w.runner(name)] == [1, 2, 3, 4]
    with pytest.raises(worker.WorkerError, match='读不了 conda 环境 nope'):
        worker.register_runner(w, name, 'nope')
    with pytest.raises(worker.WorkerError, match='不是 git 仓库'):
        worker.register_runner(w, name, 'yolo26', str(tmp_path))


def test_check_job_end_to_end(api, make_worker, fake_conda, tmp_path):
    w = make_worker()
    r = runner(w, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    assert job['status'] == 'queued' and job['runner'] == r['ref']
    assert worker.run_worker(w, quiet, once=True) == 1
    done = api.job(job['id'])
    assert done['status'] == 'done' and done['result']['same_env'] and done['result']['same_src']
    assert [h['outcome'] for h in done['history']] == ['done'] and done['history'][0]['worker'] == w.name
    log = ''.join(p['text'] for p in api.job_log(job['id'])['pieces'])
    assert '包清单和登记时一样' in log
    fake_conda(r['conda_env'], '@EXPLICIT\nhttps://x/python-3.11.tar.bz2\n')  # someone changed the environment
    job = api.submit_job('check', runner=r['runner'])
    worker.run_worker(w, quiet, once=True)
    assert api.job(job['id'])['result']['same_env'] is False
    fake_conda(r['conda_env'], '')
    (tmp_path / 'listings' / f'{r["conda_env"]}.txt').unlink()  # the environment was removed
    job = api.submit_job('check', runner=r['runner'])
    worker.run_worker(w, quiet, once=True)
    failed = api.job(job['id'])
    assert failed['status'] == 'failed' and '读不了 conda 环境' in failed['error']


def test_jobs_go_only_to_the_runners_machine(api, make_worker, fake_conda):
    a, b = make_worker(), make_worker()
    r = runner(a, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    assert b.claim() is None
    got = a.claim()
    assert got['id'] == job['id'] and got['attempt'] == 1 and got['token']


def test_a_lost_lease_goes_back_to_the_queue_and_its_token_is_refused(api, make_service, make_worker, fake_conda,
                                                                      sql):
    svc = make_service(job_lease_seconds=1)
    w = make_worker(svc)
    r = runner(w, fake_conda)
    algo = Api(svc.url, api.http.headers['authorization'].split(' ', 1)[1])
    job = algo.submit_job('check', runner=r['runner'])
    first = w.claim()
    assert first['id'] == job['id']
    w.add_log(job['id'], first['token'], 0, 'started\n')
    time.sleep(1.5)  # the machine went quiet longer than the lease
    for call in (lambda: w.renew(job['id'], first['token']),
                 lambda: w.add_log(job['id'], first['token'], 1, 'late\n'),
                 lambda: w.finish(job['id'], first['token'], 'done',
                                  {'env_sha256': '0' * 64, 'same_env': True, 'same_src': True})):
        with pytest.raises(ApiError) as e:
            call()
        assert e.value.status == 409
    assert algo.job(job['id'])['status'] == 'queued'
    second = w.claim()
    assert second['id'] == job['id'] and second['attempt'] == 2 and second['token'] != first['token']
    with pytest.raises(ApiError):
        w.renew(job['id'], first['token'])  # the first claim stays dead though the job runs again
    w.renew(job['id'], second['token'])
    history = algo.job(job['id'])['history']
    assert [h['outcome'] for h in history] == ['lost', None]
    assert [p['text'] for p in algo.job_log(job['id'], attempt=1)['pieces']] == ['started\n']
    algo.close()


def test_a_lost_claim_is_taken_back_though_nobody_looks(api, make_service, make_worker, fake_conda, sql):
    """Found reading the code before the real-machine tests: leases were only checked when someone asked for
    a job, the list or a claim; with the only worker gone and nobody looking, the job stayed 'running'."""
    svc = make_service(job_lease_seconds=1)
    w = make_worker(svc)
    r = runner(w, fake_conda)
    algo = Api(svc.url, api.http.headers['authorization'].split(' ', 1)[1])
    job = algo.submit_job('check', runner=r['runner'])
    algo.close()
    assert w.claim()['id'] == job['id']
    state = None
    for _ in range(80):  # nothing is asked of the service meanwhile
        time.sleep(0.1)
        state = sql.execute('SELECT j.status, a.outcome FROM jobs j JOIN job_attempts a ON a.job_id = j.id '
                            'WHERE j.id = %s', (job['id'],)).fetchone()
        if state['status'] != 'running':
            break
    assert state == {'status': 'queued', 'outcome': 'lost'}


def test_a_lost_claim_is_taken_back_the_moment_someone_asks(api, make_worker, fake_conda, sql):
    """The other way a lost claim is taken back, at once and not at the next round of the timer (15 seconds
    with the lease of a minute this service has)."""
    w = make_worker()
    r = runner(w, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    assert w.claim()['id'] == job['id']
    sql.execute("UPDATE jobs SET lease_expires = now() - interval '1 second' WHERE id = %s", (job['id'],))
    seen = api.job(job['id'])
    assert seen['status'] == 'queued' and [h['outcome'] for h in seen['history']] == ['lost']
    api.cancel_job(job['id'])  # not left in the queue of the service the other tests share


def test_the_folder_in_use_says_which_setting_to_change(tmp_path):
    for name, setting in (('job-5-1', 'FW_WORK_DIR'), ('build-' + 'ab' * 32 + '-7', 'FW_CACHE')):
        with worker.held_folder(tmp_path / name):
            with pytest.raises(worker.WorkerError, match=setting):
                with worker.held_folder(tmp_path / name):
                    pass


def test_tried_too_often_fails(api, make_service, make_worker, fake_conda):
    svc = make_service(job_lease_seconds=1)
    w = make_worker(svc)
    r = runner(w, fake_conda)
    algo = Api(svc.url, api.http.headers['authorization'].split(' ', 1)[1])
    job = algo.submit_job('check', runner=r['runner'])
    for attempt in (1, 2, 3):
        assert w.claim()['attempt'] == attempt
        time.sleep(1.3)
    assert w.claim() is None
    j = algo.job(job['id'])
    assert j['status'] == 'failed' and '领取了 3 次' in j['error'] and j['attempts'] == 3
    algo.close()


def test_cancel(api, make_service, make_worker, fake_conda):
    svc = make_service(job_lease_seconds=3)  # renewed every second
    w = make_worker(svc)
    r = runner(w, fake_conda)
    algo = Api(svc.url, api.http.headers['authorization'].split(' ', 1)[1])
    queued = algo.submit_job('check', runner=r['runner'])
    assert algo.cancel_job(queued['id'])['status'] == 'cancelled'
    assert algo.cancel_job(queued['id'])['status'] == 'cancelled'  # a retried cancel is no error

    job = algo.submit_job('check', runner=r['runner'])
    started = threading.Event()

    def slow(run):
        started.set()
        run.run(['sh', '-c', 'echo going; sleep 60 & wait'])  # the sleep is a child: killed with its group
        return {'env_sha256': '0' * 64, 'same_env': True, 'same_src': True}

    outcome = {}
    t = threading.Thread(target=lambda: outcome.update(
        n=worker.run_worker(w, quiet, once=True, handlers={'check': slow})))
    t.start()
    assert started.wait(10)
    time.sleep(0.5)
    t0 = time.monotonic()
    assert algo.cancel_job(job['id'])['cancel_requested']
    t.join(30)
    assert not t.is_alive() and time.monotonic() - t0 < 10
    j = algo.job(job['id'])
    assert j['status'] == 'cancelled' and j['history'][0]['outcome'] == 'cancelled'
    assert 'going' in ''.join(p['text'] for p in algo.job_log(job['id'])['pieces'])
    assert not running('sleep 60')
    algo.close()


def test_cancelled_while_its_worker_is_gone(api, make_service, make_worker, fake_conda):
    svc = make_service(job_lease_seconds=1)
    w = make_worker(svc)
    r = runner(w, fake_conda)
    algo = Api(svc.url, api.http.headers['authorization'].split(' ', 1)[1])
    job = algo.submit_job('check', runner=r['runner'])
    w.claim()
    algo.cancel_job(job['id'])
    time.sleep(1.3)
    assert algo.job(job['id'])['status'] == 'cancelled'  # not queued again
    assert w.claim() is None
    algo.close()


def test_worker_stops_when_its_lease_is_taken_back(api, make_service, make_worker, fake_conda, sql):
    svc = make_service(job_lease_seconds=3)
    w = make_worker(svc)
    r = runner(w, fake_conda)
    algo = Api(svc.url, api.http.headers['authorization'].split(' ', 1)[1])
    job = algo.submit_job('check', runner=r['runner'])
    started = threading.Event()
    runs = []

    def slow(run):
        runs.append(run.job['attempt'])
        if len(runs) == 1:
            started.set()
            run.run(['sleep', '60'])
        return {'env_sha256': '0' * 64, 'same_env': True, 'same_src': True}

    t = threading.Thread(target=lambda: worker.run_worker(w, quiet, once=True, handlers={'check': slow}))
    t.start()
    assert started.wait(10)
    # as if the network had been away for longer than the lease: the platform took the job back
    sql.execute("UPDATE jobs SET lease_expires = now() - interval '1 second' WHERE id = %s", (job['id'],))
    t.join(30)
    assert not t.is_alive()
    j = algo.job(job['id'])  # the first claim stopped its command and reported nothing; the job ran again
    assert runs == [1, 2] and j['status'] == 'done' and [h['outcome'] for h in j['history']] == ['lost', 'done']
    assert not running('sleep 60')
    algo.close()


def test_finish_is_safe_to_repeat_and_final(api, make_worker, fake_conda, sql):
    w = make_worker()
    r = runner(w, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    c = w.claim()
    result = {'env_sha256': '1' * 64, 'same_env': False, 'same_src': True}
    with pytest.raises(ApiError, match='哈希'):
        w.finish(job['id'], c['token'], 'done', {'same_env': True})
    with pytest.raises(ApiError, match='要写原因'):
        w.finish(job['id'], c['token'], 'failed')
    assert w.add_log(job['id'], c['token'], 0, 'a\n')['stored']
    assert not w.add_log(job['id'], c['token'], 0, 'a\n')['stored']  # a retried piece is kept once
    assert w.finish(job['id'], c['token'], 'done', result)['status'] == 'done'
    assert w.finish(job['id'], c['token'], 'done', result)['status'] == 'done'  # the answer was lost: send again
    with pytest.raises(ApiError) as e:
        w.finish(job['id'], c['token'], 'failed', error='x')
    assert e.value.status == 409
    with pytest.raises(ApiError, match='已经完成，不能取消') as e:
        api.cancel_job(job['id'])
    assert e.value.status == 409
    with pytest.raises(psycopg.errors.RaiseException, match='finished'):
        sql.execute("UPDATE jobs SET status = 'queued', finished_at = NULL WHERE id = %s", (job['id'],))
    with pytest.raises(psycopg.errors.RaiseException):
        sql.execute('DELETE FROM jobs WHERE id = %s', (job['id'],))
    with pytest.raises(psycopg.errors.RaiseException):
        sql.execute("UPDATE runner_versions SET conda_env = 'x' WHERE id = %s", (r['id'],))


def test_two_workers_never_get_the_same_job(api, service, make_worker, fake_conda, sql):
    w = make_worker()
    r = runner(w, fake_conda)
    ids = {api.submit_job('check', runner=r['runner'])['id'] for _ in range(6)}
    tokens = auth.create_token(sql, w.name, 'second process')[0]
    apis = [w] + [Api(service.url, tokens) for _ in range(3)]
    got, lock = [], threading.Lock()

    def take(a):
        while True:
            job = a.claim()
            if job is None:
                return
            with lock:
                got.append(job['id'])

    threads = [threading.Thread(target=take, args=(a,)) for a in apis]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(got) == sorted(ids)
    for a in apis[1:]:
        a.close()


def test_command_line(service, tokens, make_worker, fake_conda, tmp_path, monkeypatch, capsys):
    w = make_worker()
    monkeypatch.setenv('FW_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setenv('FW_CACHE', str(tmp_path / 'cache'))
    name = f'cli-{uuid.uuid4().hex[:6]}'
    fake_conda('envx', '@EXPLICIT\n')
    worker_token = w.http.headers['authorization'].split(' ', 1)[1]
    assert fw_cli.main(['login', '--server', service.url, '--token', worker_token]) == 0
    assert fw_cli.main(['runner', 'register', name, '--env', 'envx']) == 0
    assert f'已登记 {name}@1' in capsys.readouterr().out
    assert fw_cli.main(['runner', 'register', name, '--env', 'envx']) == 0
    assert '没有新版本' in capsys.readouterr().out
    assert fw_cli.main(['runner', 'check', name]) == 1  # a worker does not submit
    assert fw_cli.main(['login', '--server', service.url, '--token', tokens['algo']]) == 0
    assert fw_cli.main(['worker', '--once']) == 1
    assert 'worker 账号' in capsys.readouterr().err
    assert fw_cli.main(['runner', 'check', name]) == 0
    job_id = int(capsys.readouterr().out.split('已提交任务 ')[1].split('：')[0])
    assert fw_cli.main(['job', 'list']) == 0
    assert '排队' in capsys.readouterr().out
    assert fw_cli.main(['login', '--server', service.url, '--token', worker_token]) == 0
    assert fw_cli.main(['worker', '--once']) == 0
    assert f'领取任务 {job_id}' in capsys.readouterr().out
    assert fw_cli.main(['job', 'show', str(job_id)]) == 0
    out = capsys.readouterr().out
    assert '完成' in out and '包清单没变' in out
    assert fw_cli.main(['job', 'log', str(job_id), '--follow']) == 0
    assert '包清单和登记时一样' in capsys.readouterr().out
    assert fw_cli.main(['runner', 'list']) == 0
    assert name in capsys.readouterr().out


# --- review of PR #12 ------------------------------------------------------------------------------------
def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie no one reaps (its parent is gone too) is not running anything
        with open(f'/proc/{pid}/stat') as f:
            return f.read().split(') ')[1][0] != 'Z'
    except OSError:
        return False


def test_cancel_stops_a_real_check_with_its_children(api, make_service, make_worker, fake_conda):
    """The package listing of a check runs like any command: cancelling kills conda and what it started."""
    svc = make_service(job_lease_seconds=3)
    w = make_worker(svc)
    r = runner(w, fake_conda)
    algo = Api(svc.url, api.http.headers['authorization'].split(' ', 1)[1])
    (fake_conda.listings / f'{r["conda_env"]}.hang').write_text('')
    job = algo.submit_job('check', runner=r['runner'])
    t = threading.Thread(target=lambda: worker.run_worker(w, quiet, once=True))
    t.start()
    pids = fake_conda.listings / f'{r["conda_env"]}.pids'
    for _ in range(100):
        if pids.exists() and pids.read_text().strip():
            break
        time.sleep(0.1)
    parent, child = map(int, pids.read_text().split())
    assert alive(parent) and alive(child)
    t0 = time.monotonic()
    algo.cancel_job(job['id'])
    t.join(30)
    assert not t.is_alive() and time.monotonic() - t0 < 10
    assert algo.job(job['id'])['status'] == 'cancelled'
    assert not alive(parent) and not alive(child)
    algo.close()


def test_what_a_command_leaves_running_never_holds_up_the_job(api, make_worker, fake_conda):
    """A command that ended but left a child holding its output: the job goes on at once, the child is killed."""
    w = make_worker()
    r = runner(w, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    took = {}

    def leaves_a_child(run):
        t0 = time.monotonic()
        code = run.run(['sh', '-c', 'sleep 62 & echo started'])
        took['s'] = time.monotonic() - t0
        assert code == 0
        return {'env_sha256': '0' * 64, 'same_env': True, 'same_src': True}

    worker.run_worker(w, quiet, once=True, handlers={'check': leaves_a_child})
    assert took['s'] < 5 and api.job(job['id'])['status'] == 'done'
    assert 'started' in ''.join(p['text'] for p in api.job_log(job['id'])['pieces'])
    assert not running('sleep 62')


def test_a_worker_cut_off_from_the_platform_stops_when_its_lease_runs_out(api, make_service, make_worker,
                                                                          fake_conda):
    """Renewing and logging fail (the network is down): by its own clock the claim ends with the lease, before
    the platform can give the job to another worker, and its command is killed."""
    svc = make_service(job_lease_seconds=3)
    w = make_worker(svc)
    r = runner(w, fake_conda)
    algo = Api(svc.url, api.http.headers['authorization'].split(' ', 1)[1])
    job = algo.submit_job('check', runner=r['runner'])
    t0 = time.monotonic()
    claimed = w.claim()

    def slow(run):
        def down(*a, **k):
            raise httpx.ConnectError('network is down')
        run.api = type('Down', (), {'renew': down, 'add_log': down, 'finish': down})()
        run.run(['sh', '-c', 'echo $$ > "$0"; sleep 63', str(fake_conda.listings / 'cmd.pid')])
        return {'env_sha256': '0' * 64, 'same_env': True, 'same_src': True}

    outcome = worker.JobRun(w, claimed, quiet, claimed_at=t0).execute({'check': slow})
    assert outcome == 'lost' and time.monotonic() - t0 < 3 + 2
    assert not alive(int((fake_conda.listings / 'cmd.pid').read_text()))
    time.sleep(0.5)  # the platform's own clock: the lease ends there too, and the job can be claimed again
    again = w.claim()
    assert again['id'] == job['id'] and again['attempt'] == 2
    algo.close()


def test_a_request_that_waited_for_the_lock_past_its_lease_is_refused(
        api, make_service, make_worker, fake_conda, database):
    svc = make_service(job_lease_seconds=2)
    w = make_worker(svc)
    r = runner(w, fake_conda)
    algo = Api(svc.url, api.http.headers['authorization'].split(' ', 1)[1])
    job = algo.submit_job('check', runner=r['runner'])
    c = w.claim()
    answers = {}
    with db.connect(database) as other:
        with other.transaction():
            other.execute('SELECT id FROM jobs WHERE id = %s FOR UPDATE', (job['id'],))
            t = threading.Thread(target=lambda: answers.update(renew=status_of(lambda: w.renew(job['id'], c['token']))))
            t.start()
            time.sleep(2.5)  # the renew is waiting for the row; meanwhile the lease ends
        t.join(30)
    assert answers['renew'] == 409
    for call in (lambda: w.add_log(job['id'], c['token'], 0, 'x\n'),
                 lambda: w.finish(job['id'], c['token'], 'done',
                                  {'env_sha256': '0' * 64, 'same_env': True, 'same_src': True})):
        assert status_of(call) == 409
    algo.close()


def status_of(call):
    try:
        call()
    except ApiError as e:
        return e.status
    return 200


def test_an_accepted_cancel_is_never_overwritten_by_a_result(api, make_worker, fake_conda):
    w = make_worker()
    r = runner(w, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    c = w.claim()
    assert api.cancel_job(job['id'])['cancel_requested']
    result = {'env_sha256': '0' * 64, 'same_env': True, 'same_src': True}
    j = w.finish(job['id'], c['token'], 'done', result)  # the command ended before the worker heard of the cancel
    assert j['status'] == 'cancelled' and j['result'] is None and j['history'][0]['outcome'] == 'cancelled'
    assert w.finish(job['id'], c['token'], 'done', result)['status'] == 'cancelled'  # a retry, no error
    job = api.submit_job('check', runner=r['runner'])
    c = w.claim()
    api.cancel_job(job['id'])
    j = w.finish(job['id'], c['token'], 'failed', error='conda 坏了')
    assert j['status'] == 'cancelled' and j['error'] == 'conda 坏了'


def test_a_runner_sees_a_second_uncommitted_change_and_pip_packages(api, make_worker, fake_conda, tmp_path):
    w = make_worker()
    name = f'src-{uuid.uuid4().hex[:6]}'
    fake_conda('envs', '@EXPLICIT\n')
    src = git_repo(tmp_path / 'src')
    (src / 'f.py').write_text('x = 2\n')
    r1 = worker.register_runner(w, name, 'envs', str(src))
    assert r1['src_dirty'] and len(r1['src_state_sha256']) == 64
    assert not worker.register_runner(w, name, 'envs', str(src))['created']
    (src / 'f.py').write_text('x = 3\n')  # changed again, still not committed
    job = api.submit_job('check', runner=r1['ref'])
    worker.run_worker(w, quiet, once=True)
    assert api.job(job['id'])['result']['same_src'] is False
    r2 = worker.register_runner(w, name, 'envs', str(src))
    assert r2['created'] and r2['src_state_sha256'] != r1['src_state_sha256']
    (src / 'new_module.py').write_text('y = 1\n')  # an untracked file is part of the source too
    r3 = worker.register_runner(w, name, 'envs', str(src))
    assert r3['created'] and r3['src_state_sha256'] != r2['src_state_sha256']
    fake_conda('envs', '@EXPLICIT\n', pip='numpy==2.0.0\npip==24.0\n')  # only a pip package was upgraded
    job = api.submit_job('check', runner=r3['ref'])
    worker.run_worker(w, quiet, once=True)
    assert api.job(job['id'])['result']['same_env'] is False and api.job(job['id'])['result']['same_src']
    r4 = worker.register_runner(w, name, 'envs', str(src))
    assert r4['created'] and r4['env_sha256'] != r3['env_sha256']
    assert b'numpy==2.0.0' in w.read_object(r4['env_sha256'])


def test_a_runner_listing_is_durable_and_verified(api, service, make_worker, fake_conda, database):
    from fwserver import cli as server_cli
    from fwserver import datasets as ds
    from fwserver.store import UNSYNCED
    w = make_worker()
    r = runner(w, fake_conda, listing=f'@EXPLICIT\nhttps://x/only-here-{uuid.uuid4().hex}.tar.bz2\n')
    store = service.app.state.store
    assert not (store.root / UNSYNCED).exists()  # made durable before the runner was recorded
    data = store.read(r['env_sha256'])
    try:
        with db.connect(database) as c:
            assert not [p for p in ds.verify(c, store, echo=quiet, more=server_cli.verify_inputs) if r['ref'] in p]
            store.path(r['env_sha256']).unlink()
            problems = [p for p in ds.verify(c, store, echo=quiet, more=server_cli.verify_inputs) if r['ref'] in p]
        assert problems and '包清单' in problems[0]
    finally:
        store.put_bytes(data)  # the other tests verify this store too
        store.flush()


def test_following_the_log_across_a_second_claim(api, make_worker, fake_conda, database, capsys):
    w = make_worker()
    r = runner(w, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    first = w.claim()
    w.add_log(job['id'], first['token'], 0, 'first claim\n')
    real, calls = api.job_log, []

    def job_log(job_id, attempt=0, after=-1):
        calls.append(after)
        if len(calls) == 2:  # between two reads the first claim is lost and the job claimed again
            with db.connect(database) as c:
                c.execute("UPDATE jobs SET lease_expires = now() - interval '1 second' WHERE id = %s", (job_id,))
            second = w.claim()
            w.add_log(job_id, second['token'], 0, 'second claim\n')
            w.finish(job_id, second['token'], 'failed', error='x')
        return real(job_id, attempt, after)

    api.job_log = job_log
    fw_cli.cmd_job_log(argparse.Namespace(id=job['id'], attempt=0, follow=True), api)
    out = capsys.readouterr().out
    assert 'first claim' in out and '第 2 次领取' in out and 'second claim' in out


def test_a_log_piece_whose_answer_was_lost_keeps_its_number(api, make_worker, fake_conda):
    w = make_worker()
    r = runner(w, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    run = worker.JobRun(w, w.claim(), quiet)
    calls = []

    class AnswerLost:
        def add_log(self, job_id, token, seq, text):
            calls.append(seq)
            w.add_log(job_id, token, seq, text)  # the platform stores it...
            if len(calls) == 1:
                raise httpx.ReadTimeout('...but its answer never arrives')

    run.api = AnswerLost()
    run.log('one')
    with pytest.raises(httpx.ReadTimeout):
        run._send_log()
    run.log('two')
    run._send_log()
    assert [p['text'] for p in api.job_log(job['id'])['pieces']] == ['one\n', 'two\n']


# --- re-review of PR #12 ---------------------------------------------------------------------------------
def test_a_runner_listing_keeps_no_credentials(api, make_worker, fake_conda):
    """pip freeze keeps a private index's URL with its token and conda keeps /t/<token>/: the stored listing
    (other accounts can read it) has neither, and the check compares the listing the same way."""
    w = make_worker()
    name = f'priv-{uuid.uuid4().hex[:6]}'
    fake_conda(name, '@EXPLICIT\nhttps://conda.example/t/tk-SECRET1/main/linux-64/a-1.0-0.tar.bz2#0123abcd\n',
               pip='numpy==1.26.4\n'
                   'mylib @ https://bob:PASSWORD2@pypi.example/mylib-1.0-py3-none-any.whl?token=SECRET3#sha256=ab12\n'
                   'other @ git+https://SECRET4@git.example/other.git@0123456789abcdef\n')
    r = worker.register_runner(w, name, name, '')
    stored = w.read_object(r['env_sha256'])
    for secret in (b'SECRET1', b'PASSWORD2', b'bob', b'SECRET3', b'SECRET4'):
        assert secret not in stored
    assert b'https://pypi.example/mylib-1.0-py3-none-any.whl#sha256=ab12' in stored
    assert b'https://conda.example/t/<token>/main/linux-64/a-1.0-0.tar.bz2#0123abcd' in stored
    assert b'git.example/other.git@0123456789abcdef' in stored
    job = api.submit_job('check', runner=r['ref'])
    worker.run_worker(w, quiet, once=True)
    assert api.job(job['id'])['result']['same_env'] is True


def test_cancel_stops_a_check_whose_git_hangs(api, make_service, make_worker, fake_conda, tmp_path, monkeypatch):
    """The check reads the source with git like any command: a git diff that hangs (a slow disk, a filter) is
    killed by the cancel, and the job ends at once."""
    import shutil
    svc = make_service(job_lease_seconds=3)
    w = make_worker(svc)
    name = f'git-{uuid.uuid4().hex[:6]}'
    fake_conda(name, '@EXPLICIT\n')
    src = git_repo(tmp_path / 'src')
    r = worker.register_runner(w, name, name, str(src))
    real = shutil.which('git')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    pids = tmp_path / 'git.pids'
    (bin_dir / 'git').write_text('#!/bin/sh\n'
                                 'for a in "$@"; do [ "$a" = diff ] && '
                                 f'{{ sleep 64 & echo "$$ $!" > "{pids}"; wait; exit 0; }}; done\n'
                                 f'exec "{real}" "$@"\n')
    (bin_dir / 'git').chmod(0o755)
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
    algo = Api(svc.url, api.http.headers['authorization'].split(' ', 1)[1])
    job = algo.submit_job('check', runner=r['ref'])
    t = threading.Thread(target=lambda: worker.run_worker(w, quiet, once=True))
    t.start()
    for _ in range(100):
        if pids.exists() and pids.read_text().strip():
            break
        time.sleep(0.1)
    parent, child = map(int, pids.read_text().split())
    t0 = time.monotonic()
    algo.cancel_job(job['id'])
    t.join(30)
    assert not t.is_alive() and time.monotonic() - t0 < 10
    assert algo.job(job['id'])['status'] == 'cancelled'
    assert not alive(parent) and not alive(child)
    algo.close()


def test_the_end_of_a_job_waits_for_a_log_request_still_on_its_way(api, make_worker, fake_conda):
    """The lease thread's log request hangs past its join: the end of the job does not send the same piece
    again beside it, and both senders finish without an error."""
    w = make_worker()
    r = runner(w, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    run = worker.JobRun(w, w.claim(), quiet)
    calls, release, errors = [], threading.Event(), []

    class Slow:
        def add_log(self, job_id, token, seq, text):
            calls.append(seq)
            if len(calls) == 1:
                release.wait(10)  # the first request takes long
            w.add_log(job_id, token, seq, text)

    run.api = Slow()
    run.log('one')

    def send():
        try:
            run._send_log()
        except Exception as e:  # noqa: BLE001 - the test reports it
            errors.append(e)

    keeper = threading.Thread(target=send)
    keeper.start()
    while not calls:
        time.sleep(0.01)
    threading.Timer(0.5, release.set).start()
    send()  # the end of the job
    keeper.join(10)
    assert not errors and calls == [0]
    assert [p['text'] for p in api.job_log(job['id'])['pieces']] == ['one\n']


def test_two_identical_finishes_at_once_both_get_the_job(api, make_worker, fake_conda, database):
    """A finish retried while the first one still waits for the row: both answer with the finished job."""
    w = make_worker()
    r = runner(w, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    c = w.claim()
    result = {'env_sha256': '0' * 64, 'same_env': True, 'same_src': True}
    answers = []
    with db.connect(database) as other:
        with other.transaction():
            other.execute('SELECT id FROM jobs WHERE id = %s FOR UPDATE', (job['id'],))
            ts = [threading.Thread(target=lambda: answers.append(
                status_of(lambda: w.finish(job['id'], c['token'], 'done', result))))
                for _ in range(2)]
            for t in ts:
                t.start()
            time.sleep(1)  # both are waiting for the row
        for t in ts:
            t.join(30)
    assert answers == [200, 200]
    assert api.job(job['id'])['status'] == 'done'


def test_the_worker_says_cancelled_when_the_cancel_won(api, make_worker, fake_conda):
    w = make_worker()
    r = runner(w, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    said = []

    def cancelled_meanwhile(run):
        api.cancel_job(job['id'])  # accepted before the result is sent, not yet heard by the worker
        return {'env_sha256': '0' * 64, 'same_env': True, 'same_src': True}

    worker.run_worker(w, said.append, once=True, handlers={'check': cancelled_meanwhile})
    assert api.job(job['id'])['status'] == 'cancelled'
    assert said[-1] == f'任务 {job["id"]}：已取消'


def test_what_the_worker_says_is_seen_at_once_when_it_is_not_on_a_terminal(service, sql, tmp_path):
    """Under systemd the output is a pipe: a line held back in a buffer is a worker that seems to say nothing."""
    import select
    name = f'gpu-{uuid.uuid4().hex[:6]}'  # a worker of its own: it must not claim a job another test queued
    auth.add_user(sql, name, 'worker')
    env = dict(os.environ, FW_SERVER=service.url, FW_TOKEN=auth.create_token(sql, name, 'journal')[0], FW_CONFIG=str(tmp_path / 'none.json'),
               FW_CACHE=str(tmp_path / 'cache'))
    env.pop('PYTHONUNBUFFERED', None)
    p = subprocess.Popen([sys.executable, '-c', 'import sys; from fw.cli import main; sys.exit(main())', 'worker'],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        ready, _, _ = select.select([p.stdout], [], [], 20)
        assert ready, 'nothing was written while the worker runs'
        assert '开始领任务' in os.read(p.stdout.fileno(), 4096).decode('utf-8')
        assert p.poll() is None  # said while it runs, not flushed as it ended
    finally:
        p.kill()
        p.wait(10)


# --- found on the real machine, 2026-11-13: what a killed worker leaves behind -------------------------------
def fw_worker(service, sql, tmp_path, **more):
    """A real `fw worker` process with an account of its own: (process, its Api)."""
    name = f'gpu-{uuid.uuid4().hex[:6]}'
    auth.add_user(sql, name, 'worker')
    token = auth.create_token(sql, name, 'killed')[0]
    env = dict(os.environ, FW_SERVER=service.url, FW_TOKEN=token, FW_CONFIG=str(tmp_path / 'none.json'),
               FW_CACHE=str(tmp_path / 'cache'), **more)
    p = subprocess.Popen([sys.executable, '-c', 'import sys; from fw.cli import main; sys.exit(main())', 'worker'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    a = Api(service.url, token)
    a.name = name
    return p, a


def test_what_a_killed_worker_left_running_is_stopped_by_the_next_worker(api, service, sql, make_worker, fake_conda,
                                                                         tmp_path):
    """A worker killed with -9 cannot stop its command (it runs in a session of its own and lives on, holding
    the GPU while the retry runs). The next worker on the machine stops it before it runs anything."""
    p, w = fw_worker(service, sql, tmp_path)
    left = []
    try:
        r = runner(w, fake_conda)
        (fake_conda.listings / f'{r["conda_env"]}.hang').write_text('')
        api.submit_job('check', runner=r['runner'])
        pids = fake_conda.listings / f'{r["conda_env"]}.pids'
        for _ in range(200):
            if pids.exists() and pids.read_text().strip():
                break
            time.sleep(0.1)
        left = list(map(int, pids.read_text().split()))
        p.kill()
        p.wait(10)
        time.sleep(0.5)
        assert all(alive(pid) for pid in left)  # the fault: nothing stopped them
        mine = subprocess.Popen(['sleep', '63'], env=dict(os.environ, **worker.owner_mark()))  # of a live worker
        left.append(mine.pid)
        said = []
        assert worker.run_worker(make_worker(), said.append, once=True) == 0
        assert not any(alive(pid) for pid in left[:2])
        assert mine.poll() is None  # what a worker that is still there runs is not touched
        assert any('2 个进程' in line for line in said)
    finally:
        p.kill()
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        w.close()


HOLD = ('import sys, time\n'
        'from pathlib import Path\n'
        'from fw import worker\n'
        'with worker.held_folder(Path(sys.argv[1])) as f:\n'
        '    f.mkdir()\n'
        '    (f / "x").write_text("x")\n'
        '    print("held", flush=True)\n'
        '    time.sleep(60)\n')


def test_the_folders_a_killed_worker_left_are_removed_by_the_next_worker(make_worker, tmp_path, monkeypatch):
    """A killed worker cannot remove its job folder (found on the real machine: one per killed claim, about
    120 MB each). The next worker removes the folders no running worker holds, and nothing else."""
    jobs = tmp_path / 'jobs'
    jobs.mkdir()
    monkeypatch.setenv('FW_WORK_DIR', str(jobs))
    dead = subprocess.Popen([sys.executable, '-c', HOLD, str(jobs / 'job-7-1')], stdout=subprocess.PIPE)
    live = subprocess.Popen([sys.executable, '-c', HOLD, str(jobs / 'job-8-1')], stdout=subprocess.PIPE)
    try:
        assert dead.stdout.readline() == b'held\n' and live.stdout.readline() == b'held\n'
        dead.kill()
        dead.wait(10)
        (jobs / 'job-3-2').mkdir()  # left by a worker older than this rule: no one holds it
        (jobs / 'job-3-2' / 'data').mkdir()
        (jobs / 'notes').mkdir()  # not a job folder
        (jobs / 'job-x').mkdir()
        said = []
        assert worker.run_worker(make_worker(), said.append, once=True) == 0
        assert sorted(p.name for p in jobs.iterdir() if not p.name.endswith('.lock')) == ['job-8-1', 'job-x', 'notes']
        assert sorted(p.name for p in jobs.glob('*.lock')) == ['job-8-1.lock']
        assert any('2 个任务目录' in line for line in said)
        # and not while the folders are being kept to look into a problem
        (jobs / 'job-9-1').mkdir()
        monkeypatch.setenv('FW_KEEP_WORK', '1')
        worker.run_worker(make_worker(), said.append, once=True)
        assert (jobs / 'job-9-1').is_dir()
    finally:
        dead.kill()
        live.kill()
        live.wait(10)


def test_a_half_built_engine_a_killed_worker_left_is_removed_by_the_next_worker(make_worker, tmp_path, monkeypatch):
    """A worker killed while onnx2engine runs leaves engines/build-<key>-<pid> with a copy of the ONNX (the
    size of the model, each time). The next worker removes those no running worker holds; engines stay."""
    monkeypatch.setenv('FW_CACHE', str(tmp_path / 'cache'))
    monkeypatch.setenv('FW_WORK_DIR', str(tmp_path / 'jobs'))
    engines = tmp_path / 'cache' / 'engines'
    engines.mkdir(parents=True)
    key = 'ab' * 32
    (engines / f'{key}.engine').write_bytes(b'engine')
    (engines / f'{key}.engine.sha256').write_text('x\n')
    dead = subprocess.Popen([sys.executable, '-c', HOLD, str(engines / f'build-{key}-11')], stdout=subprocess.PIPE)
    live = subprocess.Popen([sys.executable, '-c', HOLD, str(engines / f'build-{key}-12')], stdout=subprocess.PIPE)
    try:
        assert dead.stdout.readline() == b'held\n' and live.stdout.readline() == b'held\n'
        dead.kill()
        dead.wait(10)
        (engines / f'build-{key}-13').mkdir()   # left by a worker older than this rule
        (engines / f'build-{key}-13' / 'model.onnx').write_bytes(b'onnx')
        monkeypatch.setenv('FW_KEEP_WORK', '1')  # keeps job folders to look into; half an engine is never kept
        said = []
        assert worker.run_worker(make_worker(), said.append, once=True) == 0
        assert sorted(p.name for p in engines.iterdir()) == [f'{key}.engine', f'{key}.engine.sha256',
                                                             f'build-{key}-12', f'build-{key}-12.lock']
        assert any('2 个建到一半的引擎目录' in line for line in said)
    finally:
        dead.kill()
        live.kill()
        live.wait(10)


def test_a_job_folder_is_gone_with_its_lock_when_the_job_ends(tmp_path):
    folder = tmp_path / 'job-1-1'
    with worker.held_folder(folder) as f:
        f.mkdir()
        assert worker.sweep_folders(tmp_path) == 0 and f.is_dir()  # held by this very process
    assert list(tmp_path.iterdir()) == []
