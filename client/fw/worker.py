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


def register_runner(api: Api, name: str, env: str, src_dir: str = '') -> dict:
    """Run on the GPU machine with its worker account: records the environment's package listing (uploaded to
    the store, so a later change can be compared) and the source commit as the runner's next version."""
    listing = conda_listing(env)
    digest = api.put_bytes(listing)
    src = source_info(src_dir)
    return api.register_runner({'name': name, 'conda_env': env, 'env_sha256': digest, **src})


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
            self.api.finish(self.id, self.token, outcome, result if outcome == 'done' else None,
                            error if outcome == 'failed' else '')
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
def check_runner(run: JobRun) -> dict:
    """Is the runner on this machine still what was registered? Compares the package listing and the source."""
    p = run.job['params']
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


OUTCOME_TEXT = {'done': '完成', 'failed': '失败', 'cancelled': '已取消', 'lost': '租约失效，没交结果'}
HANDLERS: Dict[str, Callable[[JobRun], dict]] = {'check': check_runner}


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
