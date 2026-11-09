"""Job queue, leases, runners and fw worker (step 3, part 1)."""
import hashlib
import os
import stat
import subprocess
import threading
import time
import uuid

import psycopg
import pytest

from fw import cli as fw_cli
from fw import worker
from fw.api import Api, ApiError
from fwserver import auth, db

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='the worker runs on the Linux GPU machine')


def quiet(_):
    pass


@pytest.fixture
def fake_conda(tmp_path, monkeypatch):
    """A conda whose `list -n <env> --explicit` prints the file listings/<env>.txt."""
    listings = tmp_path / 'listings'
    listings.mkdir()
    script = tmp_path / 'conda'
    script.write_text('#!/bin/sh\n'
                      '[ "$1" = list ] && [ "$2" = -n ] && [ "$4" = --explicit ] || exit 2\n'
                      f'f="{listings}/$3.txt"\n'
                      '[ -f "$f" ] || { echo "EnvironmentLocationNotFound: $3" >&2; exit 1; }\n'
                      'cat "$f"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv('FW_CONDA', str(script))

    def set_env(env, text):
        (listings / f'{env}.txt').write_text(text)
    return set_env


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
    assert r1['env_sha256'] == hashlib.sha256(b'@EXPLICIT\nhttps://x/torch-2.4.tar.bz2\n').hexdigest()
    assert len(r1['src_commit']) == 40 and not r1['src_dirty'] and r1['src_dir'] == str(src.resolve())
    assert w.read_object(r1['env_sha256']) == b'@EXPLICIT\nhttps://x/torch-2.4.tar.bz2\n'  # kept for comparing
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
    assert subprocess.run(['pgrep', '-f', 'sleep 60'], capture_output=True).returncode != 0
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
    assert subprocess.run(['pgrep', '-x', '-f', 'sleep 60'], capture_output=True).returncode != 0
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
