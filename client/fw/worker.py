"""fw worker: the GPU machine's side of the job queue, and the runners (inference environments) it has.

The worker only schedules: it claims a job, keeps its lease alive, sends the output back as it comes, runs
what the job needs in the runner's conda environment, and reports the result. The lease is renewed every
third of its length from a thread of its own, so a long command does not lose it; when the platform says the
job was cancelled, or the lease was lost (the platform gave the job to another worker after this machine
went quiet), what runs is killed and this claim reports nothing more.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .api import Api, ApiError

Echo = Callable[[str], None]
LOG_EVERY = 1.0        # seconds between log pieces sent while a job runs
LOG_PIECE = 128 * 1024  # characters per piece at most


class WorkerError(Exception):
    """The job cannot be done; the message is what the platform records as why it failed."""


class Cancelled(Exception):
    pass


class LeaseLost(Exception):
    pass


def conda_executable() -> str:
    """FW_CONDA, else conda on PATH, else the CONDA_EXE an activated conda shell sets."""
    found = os.environ.get('FW_CONDA') or shutil.which('conda') or os.environ.get('CONDA_EXE')
    if not found:
        raise WorkerError('找不到 conda：装好 conda，或者用环境变量 FW_CONDA 指明 conda 的路径')
    return found


def conda_listing(env: str) -> bytes:
    """`conda list -n <env> --explicit`: every package of the environment with its exact build; its sha256 tells
    whether the environment changed since it was registered."""
    try:
        r = subprocess.run([conda_executable(), 'list', '-n', env, '--explicit'], capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        raise WorkerError(f'运行 conda list 失败：{e}') from None
    if r.returncode != 0:
        why = (r.stderr or r.stdout).decode('utf-8', 'replace').strip()[-2000:]
        raise WorkerError(f'读不了 conda 环境 {env} 的包清单（conda 返回 {r.returncode}）：{why}')
    if not r.stdout.strip():
        raise WorkerError(f'conda 环境 {env} 的包清单是空的')
    return r.stdout


def source_info(src_dir: str) -> Dict[str, object]:
    """The framework source's commit and whether tracked files have changes; '' src_dir = no source."""
    if not src_dir:
        return {'src_dir': '', 'src_commit': None, 'src_dirty': False}
    from .ops import git_info  # ops is the heavier module; only this needs it
    folder = Path(src_dir).expanduser()
    if not folder.is_dir():
        raise WorkerError(f'源码目录不存在：{folder}')
    info = git_info(folder)
    if info is None:
        raise WorkerError(f'{folder} 不是 git 仓库（或者没装 git）：源码要能看出是哪个提交')
    return {'src_dir': str(folder.resolve()), 'src_commit': info['commit'], 'src_dirty': info['dirty']}


def register_runner(api: Api, name: str, env: str, src_dir: str = '', framework: str = '') -> dict:
    """Run on the GPU machine with its worker account: records the environment's package listing (uploaded to
    the store, so a later change can be compared), the source commit and the aoi-deteval framework adapter
    (ultralytics, rfdetr; '' = the runner is not used for evaluations) as the runner's next version."""
    listing = conda_listing(env)
    digest = api.put_bytes(listing)
    src = source_info(src_dir)
    return api.register_runner({'name': name, 'conda_env': env, 'env_sha256': digest, 'framework': framework,
                                **src})


class JobRun:
    """One claim of one job: its lease, its log, and what it runs."""

    def __init__(self, api: Api, job: dict, echo: Echo):
        self.api, self.job, self.echo = api, job, echo
        self.id, self.token = job['id'], job['token']
        self.renew_every = max(1.0, job.get('lease_seconds', 60) / 3)
        self.cancel = threading.Event()
        self.lost = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.pending: List[str] = []
        self.seq = 0
        self.keeper = threading.Thread(target=self._keep, name=f'job-{self.id}-lease', daemon=True)

    # --- log and lease ---------------------------------------------------------------------------
    def log(self, text: str) -> None:
        if not text:
            return
        self.echo(text.rstrip('\n'))
        with self.lock:
            self.pending.append(text if text.endswith('\n') else text + '\n')

    def _send_log(self) -> None:
        with self.lock:
            text, self.pending = ''.join(self.pending), []
        while text and not self.lost.is_set():
            piece, text = text[:LOG_PIECE], text[LOG_PIECE:]
            try:
                self.api.add_log(self.id, self.token, self.seq, piece)
            except ApiError as e:
                if e.status == 409:
                    self.lost.set()
                    return
                raise
            self.seq += 1

    def _keep(self) -> None:
        last_renew = time.monotonic()
        while not self.stop.wait(LOG_EVERY):
            try:
                self._send_log()
                if time.monotonic() - last_renew >= self.renew_every:
                    answer = self.api.renew(self.id, self.token)
                    last_renew = time.monotonic()
                    if answer.get('cancel'):
                        self.cancel.set()
            except ApiError as e:
                if e.status == 409:
                    self.lost.set()
                    return
                self.echo(f'续租或发日志失败，稍后再试：{e}')
            except Exception as e:  # network down: keep trying until the platform answers or takes the job back
                self.echo(f'续租或发日志失败，稍后再试：{e.__class__.__name__}: {e}')

    def check(self) -> None:
        """Raise when the job should stop: cancelled, or this claim lost its lease."""
        if self.lost.is_set():
            raise LeaseLost()
        if self.cancel.is_set():
            raise Cancelled()

    # --- running a command -----------------------------------------------------------------------
    def run(self, argv: List[str], cwd: Optional[str] = None, env: Optional[dict] = None) -> int:
        """Run a command, its output going to the job's log; killed (with what it started) on cancel or lost
        lease. Returns its exit status."""
        self.check()
        self.log('$ ' + ' '.join(argv))
        kwargs = {'start_new_session': True} if os.name == 'posix' else {
            'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, **kwargs)
        except OSError as e:
            raise WorkerError(f'启动不了 {argv[0]}：{e}') from None

        def pump():
            for raw in iter(proc.stdout.readline, b''):
                self.log(raw.decode('utf-8', 'replace'))
            proc.stdout.close()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            while proc.poll() is None:
                if self.cancel.is_set() or self.lost.is_set():
                    _kill(proc)
                    reader.join(5)
                    self.check()
                time.sleep(0.2)
        except BaseException:
            _kill(proc)
            raise
        reader.join(30)
        return proc.returncode

    # --- the whole claim -------------------------------------------------------------------------
    def execute(self, handlers: Dict[str, Callable[['JobRun'], dict]]) -> str:
        """Run the job and report how it ended; returns 'done', 'failed', 'cancelled' or 'lost'."""
        self.keeper.start()
        outcome, result, error = 'failed', None, ''
        try:
            handler = handlers.get(self.job['kind'])
            if handler is None:
                raise WorkerError(f'这个 worker 不会做 {self.job["kind"]} 类型的任务（升级 fw 再试）')
            result = handler(self)
            self.check()
            outcome = 'done'
        except Cancelled:
            outcome = 'cancelled'
            self.log('任务已取消')
        except LeaseLost:
            outcome = 'lost'
        except WorkerError as e:
            error = str(e)
            self.log(f'失败：{e}')
        except Exception as e:
            error = f'worker 出错：{e.__class__.__name__}: {e}'
            self.log(traceback.format_exc())
        finally:
            self.stop.set()
            self.keeper.join(30)
        if outcome == 'lost' or self.lost.is_set():
            self.echo(f'任务 {self.id} 的租约已失效（平台已收回或交给了别的 worker），这次的结果不交')
            return 'lost'
        try:
            self._send_log()
            if self.lost.is_set():
                return 'lost'
            try:
                self.api.finish(self.id, self.token, outcome, result if outcome == 'done' else None,
                                error if outcome == 'failed' else '')
            except ApiError as e:
                if outcome != 'done' or e.status != 400:
                    raise
                # the platform refused the result (e.g. the report does not add up): the job failed, and why
                outcome = 'failed'
                self.log(f'失败：平台没有收下结果：{e.message}')
                self._send_log()
                self.api.finish(self.id, self.token, 'failed', None, f'平台没有收下结果：{e.message}')
        except ApiError as e:
            if e.status == 409:
                self.echo(f'任务 {self.id} 的租约已失效，结果没交上：{e.message}')
                return 'lost'
            raise
        return outcome


def _kill(proc: subprocess.Popen) -> None:
    """Kill the command and everything it started (it runs in its own process group / session)."""
    if proc.poll() is not None:
        return
    try:
        if os.name == 'posix':
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        proc.kill()
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        pass


# --- job kinds ----------------------------------------------------------------------------------------
def check_runner(run: JobRun, p: Optional[dict] = None) -> dict:
    """Is the runner on this machine still what was registered? Compares the package listing and the source."""
    p = p if p is not None else run.job['params']
    run.log(f'检查 conda 环境 {p["conda_env"]}')
    listing = conda_listing(p['conda_env'])
    digest = hashlib.sha256(listing).hexdigest()
    run.check()
    src = source_info(p['src_dir'])
    same_env = digest == p['env_sha256']
    same_src = (src['src_commit'], src['src_dirty']) == (p['src_commit'], p['src_dirty'])
    run.log('包清单' + ('和登记时一样' if same_env else f'变了：现在 {digest[:12]}，登记时 {p["env_sha256"][:12]}'))
    if p['src_dir']:
        now = f'{src["src_commit"]}{"（有未提交的改动）" if src["src_dirty"] else ""}'
        then = f'{p["src_commit"]}{"（有未提交的改动）" if p["src_dirty"] else ""}'
        run.log('源码' + ('和登记时一样' if same_src else f'变了：现在 {now}，登记时 {then}'))
    return {'env_sha256': digest, 'src_commit': src['src_commit'], 'src_dirty': src['src_dirty'],
            'same_env': same_env, 'same_src': same_src}


def work_dir() -> Path:
    """Where jobs lay out their files: FW_WORK_DIR, else jobs/ in the fw cache folder (the same disk as the cache,
    so laying out can use reflinks)."""
    if os.environ.get('FW_WORK_DIR'):
        return Path(os.environ['FW_WORK_DIR'])
    from .config import cache_dir
    return cache_dir() / 'jobs'


def conda_run(env: str, *argv: str) -> List[str]:
    return [conda_executable(), 'run', '-n', env, '--no-capture-output', *argv]


def deteval_sets(data: Path, images: List[str], classes: List[str], imgsz: Optional[int]) -> dict:
    """The aoi-deteval sets config: one set per folder that holds images (deteval reads a set's folder without
    going into the folders below it, and tells images apart by file name), and the group 'eval' of all of them;
    with one folder, that folder is the set 'eval'. JSON, which YAML reads as it is."""
    folders = sorted({str(Path(p).parent.as_posix()) for p in images})
    if len(folders) == 1:
        sets = {'eval': str(data / folders[0])}
        groups = {}
    else:
        width = len(str(len(folders)))
        sets = {f'd{i + 1:0{width}d}': str(data / f) for i, f in enumerate(folders)}
        groups = {'eval': list(sets)}
    cfg = {'root': str(data), 'names': list(classes), 'sets': sets}
    if groups:
        cfg['groups'] = groups
    if imgsz:
        cfg['imgsz'] = imgsz
    return cfg


def eval_model(run: JobRun) -> dict:
    """Evaluate a model version: check the runner is what was registered, lay out the weights and the test
    images, run deteval predict and deteval evaluate in the runner's conda environment, upload the output
    and hand it to the platform, which registers the evaluation report."""
    import json
    from . import ops
    from .cache import Cache
    from .contract import IMAGE_EXTS, parse_manifest

    p = run.job['params']
    runner, deteval = p['runner'], p['deteval']
    now = check_runner(run, runner)
    if not now['same_env'] or not now['same_src']:
        raise WorkerError(f'推理环境和登记时不一样了（见上面），没有评估；确认没问题后在这台机器上重新 fw runner register，'
                          f'再提交评估')
    run.check()
    folder = work_dir() / f'job-{run.id}-{run.job["attempt"]}'
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    cache = Cache()
    try:
        weights = [(path, d, s) for path, d, s in p['model']['files'] if path.startswith('weights/')][0]
        model_files = [(Path(path).name, d, s) for path, d, s in p['model']['files']]
        ops._lay_out(run.api, cache, p['model']['ref'], model_files, folder / 'model', 'auto', 4, run.log)
        run.check()
        entries = parse_manifest(run.api.read_object(p['data']['inputs_sha256']))
        images = [e[0] for e in entries if Path(e[0]).suffix.lower() in IMAGE_EXTS]
        if len(images) != p['data']['images']:
            raise WorkerError(f'输入清单里有 {len(images)} 张图，提交时是 {p["data"]["images"]} 张')
        ops._lay_out(run.api, cache, p['data']['ref'], entries, folder / 'data', 'auto', 8, run.log)
        run.check()
    finally:
        cache.close()
    cfg = deteval_sets(folder / 'data', images, p['model']['classes'], deteval.get('imgsz'))
    sets = folder / 'eval_sets.yaml'
    sets.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    name = deteval['name']
    argv = ['deteval', 'predict', '--sets', str(sets), '--weights', str(folder / 'model' / Path(weights[0]).name),
            '--name', name, '--framework', runner['framework'], '--out', str(folder / 'preds')]
    if runner['src_dir']:
        argv += ['--src', runner['src_dir']]
    if deteval.get('batch'):
        argv += ['--batch', str(deteval['batch'])]
    code = run.run(conda_run(runner['conda_env'], *argv), cwd=str(folder), env=env)
    if code != 0:
        raise WorkerError(f'deteval predict 没有成功（返回 {code}），原因见日志')
    results = folder / 'results'
    code = run.run(conda_run(runner['conda_env'], 'deteval', 'evaluate', '--sets', str(sets), '--preds',
                             str(folder / 'preds' / f'{name}.json'), '--out', str(results), '--conf-set', 'eval'),
                   cwd=str(folder), env=env)
    if code != 0:
        raise WorkerError(f'deteval evaluate 没有成功（返回 {code}），原因见日志')
    run.check()
    try:
        metrics = deteval_metrics(results, name, ops)
        info = ops._read_json_object(results / 'run_info.json', 'aoi-deteval 的 run_info', [])
        data = json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
        rows = [['metrics.json', run.api.put_bytes(data), len(data)]]
        items = ops._folder_files(results, 'files/') + [('files/eval_sets.yaml', sets),
                                                       (f'files/preds/{name}.json', folder / 'preds' / f'{name}.json')]
        rows += ops._send_files(run.api, items, 4, run.log)
    except ops.FwError as e:
        raise WorkerError(str(e)) from None
    run.log(f'评了 {metrics.get("images")} 张图，mAP50 {metrics.get("mAP50")}，mAP50-95 {metrics.get("mAP50-95")}')
    if not os.environ.get('FW_KEEP_WORK'):
        shutil.rmtree(folder, ignore_errors=True)
    return {'files': rows, 'tool_version': str(info.get('deteval_version', ''))}


def deteval_metrics(results: Path, name: str, ops) -> dict:
    """The result of the set (or group) 'eval' for this model, with the model's thresholds and settings."""
    undefined: List[str] = []
    summary = ops._read_json_object(results / 'summary.json', 'aoi-deteval 的结果', undefined)
    model = summary.get(name)
    if not isinstance(model, dict) or not isinstance(model.get('eval'), dict):
        raise WorkerError(f'aoi-deteval 的结果里没有模型 {name} 在 eval 上的指标')
    metrics = {k: model[k] for k in ops.DETEVAL_META if k in model}
    metrics.update(model['eval'])
    return metrics


OUTCOME_TEXT = {'done': '完成', 'failed': '失败', 'cancelled': '已取消', 'lost': '租约失效，没交结果'}
HANDLERS: Dict[str, Callable[[JobRun], dict]] = {'check': check_runner, 'eval': eval_model}


def run_worker(api: Api, echo: Echo, poll: float = 5.0, once: bool = False,
               handlers: Optional[Dict[str, Callable[[JobRun], dict]]] = None) -> int:
    """Claim and run jobs one after another. once: stop when the queue has nothing for this worker.
    Returns how many jobs it ran."""
    handlers = handlers or HANDLERS
    ran = 0
    idle_said = False
    while True:
        job = api.claim()
        if job is None:
            if once:
                return ran
            if not idle_said:
                echo(f'没有任务，每 {poll:g} 秒问一次')
                idle_said = True
            time.sleep(poll)
            continue
        idle_said = False
        echo(f'领取任务 {job["id"]}（{job["kind_text"]}，第 {job["attempt"]} 次）')
        outcome = JobRun(api, job, echo).execute(handlers)
        echo(f'任务 {job["id"]}：{OUTCOME_TEXT[outcome]}')
        ran += 1
        sys.stdout.flush()
