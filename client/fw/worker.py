"""fw worker: the GPU machine's side of the job queue, and the runners (inference environments) it has.

The worker only schedules: it claims a job, keeps its lease alive, sends the output back as it comes, runs
what the job needs in the runner's conda environment, and reports the result. The lease is renewed every
third of its length from a thread of its own, so a long command does not lose it; when the platform says the
job was cancelled, or the lease was lost (the platform gave the job to another worker after this machine
went quiet), what runs is killed and this claim reports nothing more.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

import httpx

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


Capture = Callable[..., Tuple[int, bytes, bytes]]  # (argv, env=None) -> (exit status, stdout, stderr)
PIP_HEADER = b'\n# pip freeze --all\n'
# What a package listing must not keep (it is stored and other accounts can read it): the user and password in
# a URL, its query (signed URLs, ?token=...) and conda's token path (/t/<token>/). The fragment stays: it is the
# package's hash (#md5=..., #sha256=...).
_URL = re.compile(rb'([A-Za-z][A-Za-z0-9+.-]*://)(?:[^/\s@]*@)?([^\s?#]*)(?:\?[^\s#]*)?')
_CONDA_TOKEN = re.compile(rb'/t/[^/\s]+/')


def redact(listing: bytes) -> bytes:
    """The listing without credentials (see _URL); the same listing always gives the same bytes."""
    return _URL.sub(lambda m: m.group(1) + _CONDA_TOKEN.sub(b'/t/<token>/', m.group(2)), listing)


def plain_capture(argv: List[str], env: Optional[dict] = None) -> Tuple[int, bytes, bytes]:
    """Outside a job (fw runner register): run and wait, at most 5 minutes."""
    try:
        r = subprocess.run(argv, capture_output=True, timeout=300, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        raise WorkerError(f'运行 {argv[0]} 失败：{e}') from None
    return r.returncode, r.stdout, r.stderr


def conda_listing(env: str, capture: Optional[Capture] = None) -> bytes:
    """What the environment holds: `conda list -n <env> --explicit` (every conda package with its exact build),
    then `pip freeze --all` run in it (packages installed with pip, which the conda listing leaves out). Its
    sha256 tells whether the environment changed since it was registered. An environment without pip gets a
    line saying so. Credentials in package URLs are left out (redact)."""
    capture = capture or plain_capture
    conda = conda_executable()
    code, out, err = capture([conda, 'list', '-n', env, '--explicit'])
    if code != 0:
        why = (err or out).decode('utf-8', 'replace').strip()[-2000:]
        raise WorkerError(f'读不了 conda 环境 {env} 的包清单（conda 返回 {code}）：{why}')
    if not out.strip():
        raise WorkerError(f'conda 环境 {env} 的包清单是空的')
    code, pip, err = capture([conda, 'run', '-n', env, 'python', '-m', 'pip', 'freeze', '--all',
                              '--disable-pip-version-check'])
    if code != 0:
        why = (err or pip).decode('utf-8', 'replace').strip()
        if 'No module named pip' not in why:
            raise WorkerError(f'读不了 conda 环境 {env} 里 pip 装的包（返回 {code}）：{why[-2000:]}')
        pip = b'# no pip in this environment\n'
    return redact(out + PIP_HEADER + pip)


def _git(capture: Capture, folder: Path, *args: str) -> Tuple[int, bytes, bytes]:
    # without GIT_* variables, which would point git at another repository than the folder asked for
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    return capture(['git', '-C', str(folder), *args], env=env)


def _git_bytes(folder: Path, *args: str, capture: Optional[Capture] = None) -> bytes:
    code, out, err = _git(capture or plain_capture, folder, *args)
    if code != 0:
        raise WorkerError(f'git {args[0]} 失败：{err.decode("utf-8", "replace").strip()[-500:]}')
    return out


def source_state(folder: Path, capture: Optional[Capture] = None) -> Optional[str]:
    """sha256 of what the working copy has beyond its commit: the diff of tracked files (git diff HEAD --binary)
    and every untracked file that is not ignored, by path and content. None when there is nothing; so a second
    change to a source that already had uncommitted changes is seen too."""
    diff = _git_bytes(folder, 'diff', 'HEAD', '--binary', '--no-color', '--no-ext-diff', capture=capture)
    listed = _git_bytes(folder, 'ls-files', '--others', '--exclude-standard', '-z', capture=capture)
    untracked = sorted(p for p in listed.split(b'\0') if p)
    if not diff and not untracked:
        return None
    h = hashlib.sha256(b'diff\0' + diff)
    for rel in untracked:
        path = folder / os.fsdecode(rel)
        try:
            content = hashlib.sha256(path.read_bytes()).hexdigest().encode() if not path.is_symlink() else \
                b'link:' + os.fsencode(os.readlink(path))
        except OSError as e:
            raise WorkerError(f'读不了源码目录里的 {path}（{e.strerror or e}）') from None
        h.update(b'\0file\0' + rel + b'\0' + content)
    return h.hexdigest()


def source_info(src_dir: str, capture: Optional[Capture] = None) -> Dict[str, object]:
    """The framework source's commit, and whether (and how) the working copy differs from it (see
    source_state); '' src_dir = no source. capture: how git is run (in a job: JobRun.capture, cancellable)."""
    if not src_dir:
        return {'src_dir': '', 'src_commit': None, 'src_dirty': False, 'src_state_sha256': None}
    capture = capture or plain_capture
    folder = Path(src_dir).expanduser()
    if not folder.is_dir():
        raise WorkerError(f'源码目录不存在：{folder}')
    try:
        code, out, _ = _git(capture, folder, 'rev-parse', 'HEAD')
    except WorkerError:
        code, out = 1, b''
    commit = out.decode('ascii', 'replace').strip().lower()
    if code != 0 or not re.fullmatch(r'[0-9a-f]{7,64}', commit):
        raise WorkerError(f'{folder} 不是 git 仓库（或者没装 git）：源码要能看出是哪个提交')
    state = source_state(folder, capture)
    return {'src_dir': str(folder.resolve()), 'src_commit': commit, 'src_dirty': state is not None,
            'src_state_sha256': state}


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def onnx2engine_info(path: str) -> Tuple[str, str]:
    """The onnx2engine program (from the deploy code) as registered: its absolute path and sha256."""
    found = shutil.which(path) if os.sep not in path else path
    program = Path(found or path).expanduser()
    if not program.is_file() or not os.access(program, os.X_OK):
        raise WorkerError(f'找不到能运行的 onnx2engine：{path}')
    return str(program.resolve()), file_sha256(program)


def register_runner(api: Api, name: str, env: str, src_dir: str = '', framework: str = '',
                    onnx2engine: str = '') -> dict:
    """Run on the GPU machine with its worker account: records the environment's package listing (uploaded to
    the store, so a later change can be compared), the source commit, the TensorRT inference adapter (rfdetr;
    '' = the runner only runs checks) and the onnx2engine program that builds engines, as the runner's next
    version."""
    if bool(framework) != bool(onnx2engine):
        raise WorkerError('--framework 和 --onnx2engine 要一起写：评估时用 onnx2engine 从 ONNX 建引擎，再用框架的推理模块跑')
    tool, tool_sha = onnx2engine_info(onnx2engine) if onnx2engine else ('', None)
    listing = conda_listing(env)
    digest = api.put_bytes(listing)  # the platform makes it durable before it records the runner
    src = source_info(src_dir)
    return api.register_runner({'name': name, 'conda_env': env, 'env_sha256': digest, 'framework': framework,
                                'onnx2engine': tool, 'onnx2engine_sha256': tool_sha, **src})


class JobRun:
    """One claim of one job: its lease, its log, and what it runs."""

    def __init__(self, api: Api, job: dict, echo: Echo, claimed_at: Optional[float] = None):
        """claimed_at: time.monotonic() before the claim was sent; the lease runs out lease_seconds after it at
        the latest, whatever this machine hears from the platform."""
        self.api, self.job, self.echo = api, job, echo
        self.id, self.token = job['id'], job['token']
        self.lease_seconds = job.get('lease_seconds', 60)
        self.renew_every = max(1.0, self.lease_seconds / 3)
        self.deadline = (time.monotonic() if claimed_at is None else claimed_at) + self.lease_seconds
        self.cancel = threading.Event()
        self.lost = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.pending: List[str] = []
        self.outbox: Deque[Tuple[int, str]] = deque()  # numbered pieces not yet confirmed by the platform
        self.sending = threading.Lock()  # one sender at a time: the lease thread, or the end of the job
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
        """Send the output so far. Each piece gets its number once and is sent again with that number until the
        platform answers, so a piece the platform stored but whose answer was lost is not stored twice, and the
        next piece never reuses its number. One sender at a time: a request of the lease thread still waiting
        for an answer when the job ends is waited for, not raced (both would send the same piece)."""
        with self.sending:
            self._send_pending()

    def _send_pending(self) -> None:
        with self.lock:
            text, self.pending = ''.join(self.pending), []
            while text:
                self.outbox.append((self.seq, text[:LOG_PIECE]))
                text = text[LOG_PIECE:]
                self.seq += 1
        while self.outbox and not self.lost.is_set():
            seq, piece = self.outbox[0]
            try:
                self.api.add_log(self.id, self.token, seq, piece)
            except ApiError as e:
                if e.status == 409:
                    self.lost.set()
                    return
                raise
            self.outbox.popleft()

    def expired(self) -> bool:
        """The lease ran out by this machine's clock (the platform could not be reached to renew it): the
        platform may already have given the job to another worker, so this claim stops."""
        if not self.lost.is_set() and time.monotonic() >= self.deadline:
            self.echo(f'任务 {self.id}：租约到期前没能续上（连不上平台），停下任务，这次领取作废')
            self.lost.set()
        return self.lost.is_set()

    def _keep(self) -> None:
        last_renew = time.monotonic()
        while not self.stop.wait(LOG_EVERY):
            if self.expired():
                return
            try:
                self._send_log()
                if time.monotonic() - last_renew >= self.renew_every:
                    sent = time.monotonic()
                    answer = self.api.renew(self.id, self.token)
                    last_renew = sent
                    self.deadline = sent + answer.get('lease_seconds', self.lease_seconds)
                    if answer.get('cancel'):
                        self.cancel.set()
            except ApiError as e:
                if e.status == 409:
                    self.lost.set()
                    return
                self.echo(f'续租或发日志失败，稍后再试：{e}')
            except Exception as e:  # network down: keep trying until the platform answers or takes the job back
                self.echo(f'续租或发日志失败，稍后再试：{e.__class__.__name__}: {e}')

    def _finish(self, outcome: str, result: Optional[dict], error: str) -> Optional[dict]:
        """Send the result; while the platform cannot take it (restarting, network down, 5xx), keep trying and keep
        the lease alive for at most FINISH_RETRY_SECONDS, then stop renewing and let the lease run out (None):
        the job goes back to the queue instead of hanging as running. A cancel heard meanwhile is sent instead."""
        delay = 2.0
        give_up = time.monotonic() + FINISH_RETRY_SECONDS
        while True:
            try:
                return self.api.finish(self.id, self.token, outcome, result, error)
            except Exception as e:  # noqa: BLE001
                if not transient(e):
                    raise
                self.echo(f'任务 {self.id}：交结果没成功（{e}），{delay:g} 秒后再试')
            if time.monotonic() + delay >= give_up:
                self.echo(f'任务 {self.id}：{FINISH_RETRY_SECONDS:g} 秒内一直交不上结果，不再续租，任务到期后回到队列')
                return None
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            try:
                sent = time.monotonic()
                answer = self.api.renew(self.id, self.token)
                self.deadline = sent + answer.get('lease_seconds', self.lease_seconds)
                if answer.get('cancel') and outcome != 'cancelled':
                    outcome, result, error = 'cancelled', None, ''  # cancelled meanwhile: that is what is sent
            except Exception as e:  # noqa: BLE001
                if not transient(e):
                    raise
            if self.expired():
                return None

    def check(self) -> None:
        """Raise when the job should stop: cancelled, or this claim lost its lease."""
        if self.expired():
            raise LeaseLost()
        if self.cancel.is_set():
            raise Cancelled()

    # --- running a command -----------------------------------------------------------------------
    def run(self, argv: List[str], cwd: Optional[str] = None, env: Optional[dict] = None) -> int:
        """Run a command, its output going to the job's log. Returns its exit status (see _spawn)."""
        self.log('$ ' + ' '.join(argv))
        return self._spawn(argv, cwd, env, lambda raw: self.log(raw.decode('utf-8', 'replace')), None)

    def capture(self, argv: List[str], env: Optional[dict] = None) -> Tuple[int, bytes, bytes]:
        """Run a command for its output (not logged): (exit status, stdout, stderr); see _spawn."""
        out: List[bytes] = []
        err: List[bytes] = []
        code = self._spawn(argv, None, env, out.append, err.append)
        return code, b''.join(out), b''.join(err)

    def _spawn(self, argv: List[str], cwd: Optional[str], env: Optional[dict], on_out: Callable[[bytes], None],
               on_err: Optional[Callable[[bytes], None]]) -> int:
        """Run a command in a process group (a session on Linux) of its own. On cancel or a lost lease the whole
        group is killed and Cancelled / LeaseLost raised. When the command itself ends, whatever it left running
        in its group is killed too: nothing outlives the job, and nothing left over keeps its output pipe open."""
        self.check()
        kwargs = {'start_new_session': True} if os.name == 'posix' else {
            'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
        env = dict(os.environ if env is None else env, **owner_mark())  # see sweep_processes
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE if on_err else subprocess.STDOUT, **kwargs)
        except OSError as e:
            raise WorkerError(f'启动不了 {argv[0]}：{e}') from None

        def pump(stream, sink):
            for raw in iter(stream.readline, b''):
                sink(raw)
            stream.close()

        readers = [threading.Thread(target=pump, args=(proc.stdout, on_out), daemon=True)]
        if on_err:
            readers.append(threading.Thread(target=pump, args=(proc.stderr, on_err), daemon=True))
        for r in readers:
            r.start()
        try:
            while not _ended(proc):
                if self.cancel.is_set() or self.expired():
                    raise LeaseLost() if self.lost.is_set() else Cancelled()
                time.sleep(0.2)
            _kill(proc)  # what it left in its group (the group lives on while the ended leader is not reaped)
            for r in readers:
                while r.is_alive():
                    r.join(0.2)
                    if self.cancel.is_set() or self.expired():  # Windows: a leftover may still hold the pipe
                        raise LeaseLost() if self.lost.is_set() else Cancelled()
        except BaseException:
            _kill(proc)
            for r in readers:
                r.join(5)
            raise
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
            try:
                self._send_log()
            except Exception as e:  # noqa: BLE001 - the result matters more than the last lines of log
                if not transient(e):
                    raise
                self.echo(f'任务 {self.id}：最后一段日志没传上：{e}')
            if self.lost.is_set():
                return 'lost'
            try:
                job = self._finish(outcome, result if outcome == 'done' else None,
                                   error if outcome == 'failed' else '')
            except ApiError as e:
                if outcome != 'done' or e.status != 400:
                    raise
                # the platform refused the result (e.g. the report does not add up): the job failed, and why
                outcome = 'failed'
                self.log(f'失败：平台没有收下结果：{e.message}')
                self._send_log()
                job = self._finish('failed', None, f'平台没有收下结果：{e.message}')
            if job is None:
                self.echo(f'任务 {self.id}：租约到期前一直连不上平台，结果没交上，任务会回到队列')
                return 'lost'
        except ApiError as e:
            if e.status == 409:
                self.echo(f'任务 {self.id} 的租约已失效，结果没交上：{e.message}')
                return 'lost'
            raise
        # what the platform recorded: a cancel accepted before the result came wins over it
        status = job.get('status') if isinstance(job, dict) else None
        return status if status in ('done', 'failed', 'cancelled') else outcome


def _ended(proc: subprocess.Popen) -> bool:
    """Has the command itself ended? On Linux without reaping it: an ended but unreaped leader keeps its
    process group id taken, so killing the group afterwards cannot reach anything else."""
    if proc.returncode is not None:
        return True
    if os.name == 'posix':
        try:
            info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return proc.poll() is not None
        return info is not None
    return proc.poll() is not None


def _kill(proc: subprocess.Popen) -> None:
    """Kill the command and everything it started (it runs in its own process group / session), then reap it."""
    try:
        if os.name == 'posix':
            os.killpg(proc.pid, signal.SIGKILL)
        elif proc.returncode is None:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        if proc.returncode is None:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        pass


# --- job kinds ----------------------------------------------------------------------------------------
def check_runner(run: JobRun, p: Optional[dict] = None) -> dict:
    """Is the runner on this machine still what was registered? Compares the package listing and the source."""
    p = p if p is not None else run.job['params']
    run.log(f'检查 conda 环境 {p["conda_env"]}')
    listing = conda_listing(p['conda_env'], run.capture)
    digest = hashlib.sha256(listing).hexdigest()
    run.check()
    src = source_info(p['src_dir'], run.capture)
    run.check()
    same_env = digest == p['env_sha256']
    if p.get('onnx2engine'):
        try:
            tool = file_sha256(Path(p['onnx2engine']))
        except OSError as e:
            tool = f'读不了（{e.strerror or e}）'
        same_tool = tool == p['onnx2engine_sha256']
        run.log(f'onnx2engine {p["onnx2engine"]}' + ('和登记时一样' if same_tool else f'变了：现在 {tool[:12]}'))
        same_env = same_env and same_tool  # the program is part of what the environment runs
    same_src = (src['src_commit'], src['src_state_sha256']) == (p['src_commit'], p.get('src_state_sha256'))
    run.log('包清单' + ('和登记时一样' if same_env else f'变了：现在 {digest[:12]}，登记时 {p["env_sha256"][:12]}'))
    if p['src_dir']:
        now = f'{src["src_commit"]}{"（有未提交的改动）" if src["src_dirty"] else ""}'
        then = f'{p["src_commit"]}{"（有未提交的改动）" if p["src_dirty"] else ""}'
        run.log('源码' + ('和登记时一样' if same_src else f'变了：现在 {now}，登记时 {then}'))
    return {'env_sha256': digest, 'src_commit': src['src_commit'], 'src_dirty': src['src_dirty'],
            'src_state_sha256': src['src_state_sha256'], 'same_env': same_env, 'same_src': same_src}


def work_dir() -> Path:
    """Where jobs lay out their files: FW_WORK_DIR, else jobs/ in the fw cache folder (the same disk as the cache,
    so laying out can use reflinks)."""
    if os.environ.get('FW_WORK_DIR'):
        return Path(os.environ['FW_WORK_DIR']).expanduser().resolve()  # commands run with the job's folder as cwd
    from .config import cache_dir
    return (cache_dir() / 'jobs').resolve()


# --- what a worker that was killed left behind ----------------------------------------------------------
# A worker killed with -9 (or by the OOM killer) stops nothing and removes nothing: its command runs in a
# session of its own and lives on, and its job folder stays. Under systemd the service's cgroup takes the
# command down with it; a worker started by hand has no such thing. So every command carries the worker that
# started it in its environment, every job folder is held with a lock that dies with the worker, and a worker
# clears what dead workers left on this machine when it starts and each time it claims a job.
OWNER_ENV = 'FW_WORKER_OWNER'
JOB_FOLDER = re.compile(r'job-\d+-\d+')


def _started(pid: int) -> Optional[str]:
    """When a process started (clock ticks since boot, from /proc), None when it is not there (or no /proc).
    With the pid it names one process for good: a pid is used again, a start time with it is not."""
    try:
        with open(f'/proc/{pid}/stat', 'rb') as f:
            fields = f.read().rsplit(b') ', 1)[1].split()
    except (OSError, IndexError):
        return None
    if len(fields) < 20 or fields[0] == b'Z':
        return None
    return fields[19].decode('ascii', 'replace')


def owner_mark() -> Dict[str, str]:
    """What the commands of this worker carry in their environment: this process and when it started."""
    return {OWNER_ENV: f'{os.getpid()}:{_started(os.getpid()) or ""}'}


def _owner_gone(mark: str) -> bool:
    pid, _, started = mark.partition(':')
    if not pid.isdigit() or not started:
        return False  # not a mark of ours we can read: leave it alone
    return _started(int(pid)) != started


def sweep_processes(wait: float = 10.0) -> int:
    """Kill the commands whose worker is gone (Linux; elsewhere nothing is done). Only processes that carry a
    worker's mark and whose worker no longer runs: what a second worker on this machine runs is not touched.
    Returns how many were killed, after waiting for them to be gone (so the GPU memory is free again)."""
    proc = Path('/proc')
    if os.name != 'posix' or not proc.is_dir():
        return 0
    needle = OWNER_ENV.encode('ascii') + b'='
    killed = []
    for d in proc.iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            env = (d / 'environ').read_bytes()  # other accounts' processes cannot be read: not ours
        except OSError:
            continue
        mark = next((v[len(needle):] for v in env.split(b'\0') if v.startswith(needle)), None)
        if mark is None or not _owner_gone(mark.decode('ascii', 'replace')) or _started(int(d.name)) is None:
            continue
        try:
            os.kill(int(d.name), signal.SIGKILL)
            killed.append(int(d.name))
        except OSError:
            pass
    end = time.monotonic() + wait
    while any(_started(pid) is not None for pid in killed) and time.monotonic() < end:
        time.sleep(0.1)
    return len(killed)


def _lock_of(folder: Path) -> Path:
    return folder.with_name(folder.name + '.lock')


def _try_hold(lock: Path):
    """Take the lock of a job folder without waiting: the open file while it is ours, None when a running
    worker holds it. The lock goes when the process that holds it does, however it ends."""
    import fcntl
    while True:
        f = open(lock, 'a+b')
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return None
        try:  # the file may have been removed by whoever held it a moment ago: then this is a lock on nothing
            if os.fstat(f.fileno()).st_ino == os.stat(lock).st_ino:
                return f
        except OSError:
            pass
        f.close()


@contextlib.contextmanager
def held_folder(folder: Path):
    """A job folder for as long as the block runs: held with a lock next to it (<folder>.lock) so that another
    worker on this machine knows it is in use, removed with it at the end (kept with FW_KEEP_WORK)."""
    held = None
    if os.name == 'posix':
        held = _try_hold(_lock_of(folder))
        if held is None:
            raise WorkerError(f'任务目录 {folder} 正被这台机器上另一个 worker 用着（它连的多半是另一个平台）；'
                              f'给每个 worker 设各自的 FW_WORK_DIR 再启动')
    try:
        yield folder
    finally:
        if not os.environ.get('FW_KEEP_WORK'):
            shutil.rmtree(folder, ignore_errors=True)
        if held is not None:
            try:
                os.unlink(_lock_of(folder))
            except OSError:
                pass
            held.close()


def sweep_folders(root: Path) -> int:
    """Remove the job folders under root that no running worker holds (left by a worker that was killed or lost
    power, or by one older than the locks). Only names like job-<id>-<attempt>. Returns how many folders."""
    if os.name != 'posix' or not root.is_dir():
        return 0
    removed = 0
    names = {p.name[:-5] if p.name.endswith('.lock') else p.name for p in root.iterdir()}
    for name in sorted(n for n in names if JOB_FOLDER.fullmatch(n)):
        folder = root / name
        held = _try_hold(_lock_of(folder))
        if held is None:
            continue
        try:
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
                removed += 0 if folder.exists() else 1
            if not folder.exists():
                os.unlink(_lock_of(folder))
        except OSError:
            pass
        finally:
            held.close()
    return removed


def sweep(echo: Echo) -> None:
    """Clear what dead workers left on this machine: their commands first (they write into the folders)."""
    try:
        killed = sweep_processes()
        removed = 0 if os.environ.get('FW_KEEP_WORK') else sweep_folders(work_dir())
    except OSError as e:
        echo(f'清理上一个 worker 留下的东西时出错（{e}），继续领任务')
        return
    if killed or removed:
        echo(f'这台机器上有 worker 没来得及收场就没了：停掉了它留下的 {killed} 个进程，删掉了它留下的 {removed} 个任务目录')


def conda_run(env: str, *argv: str) -> List[str]:
    return [conda_executable(), 'run', '-n', env, '--no-capture-output', *argv]


def deteval_sets(data: Path, images: List[str], classes: List[str]) -> dict:
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
    return cfg


ENGINE_PRECISION = 'fp16'  # what onnx2engine says it builds (its own setting, not read back from the engine)
PREDICT_CONF = 0.001  # the lowest score kept in predictions (mAP needs nearly all of them, as deteval predict)
ENGINE_LOAD_FAILED = 3  # trt_predict.py's exit status when the engine cannot be loaded


def gpu_device() -> str:
    """The GPU the inference module's device 0 is: the first of CUDA_VISIBLE_DEVICES (an index or a UUID), else 0.
    The commands run with CUDA_DEVICE_ORDER=PCI_BUS_ID (gpu_env), so CUDA counts the GPUs in nvidia-smi's order."""
    first = os.environ.get('CUDA_VISIBLE_DEVICES', '').split(',')[0].strip()
    return first or '0'


def gpu_env() -> dict:
    return dict(os.environ, CUDA_DEVICE_ORDER='PCI_BUS_ID', PYTHONUNBUFFERED='1')


def gpu_info(run: JobRun) -> str:
    """The GPU the engine is built for and runs on (gpu_device) and its driver."""
    code, out, err = run.capture(['nvidia-smi', '--query-gpu=name,driver_version', '--format=csv,noheader',
                                  '-i', gpu_device()])
    text = out.decode('utf-8', 'replace').strip()
    if code != 0 or not text:
        raise WorkerError(f'nvidia-smi 读不到显卡 {gpu_device()}：{err.decode("utf-8", "replace").strip()[-300:] or code}')
    return text.splitlines()[0].strip()


_NVINFER = re.compile(r'libnvinfer\.so(?:\.\d+)*\Z')


def linked_trt_version(run: JobRun, env: str, program: str) -> Optional[str]:
    """The version of the libnvinfer the program is linked with, from the library's real file name
    (libnvinfer.so.10 -> libnvinfer.so.10.14.1), as the environment resolves it; None when that cannot be told
    (linked statically, no ldd, a library named without its version)."""
    code, out, _ = run.capture([conda_executable(), 'run', '-n', env, 'ldd', program])
    if code != 0:
        return None
    for line in out.decode('utf-8', 'replace').splitlines():
        name, arrow, rest = line.strip().partition(' => ')
        if not arrow or not _NVINFER.match(name.strip()) or not rest.startswith('/'):
            continue
        real = os.path.basename(os.path.realpath(rest.split(' (')[0].strip()))
        version = real.partition('.so.')[2]
        if version.count('.') >= 1:
            return version
    return None


def trt_version(run: JobRun, env: str, program: str = '') -> str:
    """The TensorRT that builds and runs the engine: the library onnx2engine is linked with when that can be read
    (the deploy code is C++; the tensorrt Python package may be missing or another version), else the Python
    package of the environment."""
    linked = linked_trt_version(run, env, program) if program else None
    if linked:
        return linked
    code, out, err = run.capture([conda_executable(), 'run', '-n', env, 'python', '-c',
                                  'import tensorrt; print(tensorrt.__version__)'])
    version = out.decode('utf-8', 'replace').strip().splitlines()[-1:] if code == 0 else []
    if not version:
        raise WorkerError(f'读不到 TensorRT 版本：{program or "onnx2engine"} 链接的 libnvinfer 文件名里没有完整版本号，环境 {env} '
                          f'里也没有 tensorrt 这个 Python 包（装一个和 libnvinfer 同版本的）：'
                          f'{err.decode("utf-8", "replace").strip()[-300:]}')
    return version[0].strip()


def _fsync(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _drop_engine(engine: Path) -> None:
    for p in (engine, engine.with_name(engine.name + '.sha256')):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _cached_engine(run: JobRun, engine: Path) -> Optional[str]:
    """The sha256 of a cached engine that is whole (its .sha256 file, written after it, agrees), else None; a
    cached engine that is not is removed, to be built again."""
    sidecar = engine.with_name(engine.name + '.sha256')
    if not engine.exists() and not sidecar.exists():
        return None
    try:
        want = sidecar.read_text(encoding='ascii').strip()
        have = file_sha256(engine) if engine.stat().st_size else ''
    except (OSError, UnicodeDecodeError):
        want, have = '', None
    if want and have == want:
        return have
    run.log(f'缓存里的引擎 {engine.name} 不完整或者被改过（和它的 .sha256 对不上），删掉重建')
    _drop_engine(engine)
    return None


def build_engine(run: JobRun, runner: dict, onnx: Path, onnx_sha256: str) -> Tuple[Path, dict]:
    """The engine of this ONNX on this machine: built with the runner's onnx2engine in its environment, kept in
    engines/ of the fw cache folder by ONNX, program, environment, GPU and driver (a cache: never uploaded or
    released; a site builds its own from the same ONNX, and may get a slightly different engine since TensorRT
    picks its FP16 kernels per build). The engine is written whole before it is used: fsync, rename, then its
    .sha256 file; one that does not match its .sha256 is built again. Returns its path and what it was built
    from."""
    import json
    from .onnx_check import OnnxError, check_self_contained
    try:
        check_self_contained(onnx)
    except OnnxError as e:
        raise WorkerError(f'没有建引擎：{e}') from None
    gpu = gpu_info(run)
    trt = trt_version(run, runner['conda_env'], runner['onnx2engine'])
    info = {'onnx_sha256': onnx_sha256, 'onnx2engine_sha256': runner['onnx2engine_sha256'],
            'env_sha256': runner['env_sha256'], 'gpu': gpu, 'trt_version': trt, 'precision': ENGINE_PRECISION}
    key = hashlib.sha256(json.dumps(info, sort_keys=True).encode('utf-8')).hexdigest()
    from .config import cache_dir
    cache = cache_dir() / 'engines'
    cache.mkdir(parents=True, exist_ok=True)
    engine = cache / f'{key}.engine'
    digest = _cached_engine(run, engine)
    if digest is not None:
        run.log(f'用缓存的引擎（{gpu}，TensorRT {trt}）')
    else:
        build = cache / f'build-{key}-{os.getpid()}'
        shutil.rmtree(build, ignore_errors=True)
        build.mkdir()
        try:
            shutil.copyfile(onnx, build / 'model.onnx')  # alone in its folder: nothing else to read next to it
            run.log(f'用 onnx2engine 在这台机器上建引擎（onnx2engine 设的精度 {ENGINE_PRECISION.upper()}；'
                    f'{gpu}，TensorRT {trt}）')
            code = run.run(conda_run(runner['conda_env'], runner['onnx2engine'], str(build / 'model.onnx')),
                           cwd=str(build), env=gpu_env())
            # onnx2engine writes the engine next to the ONNX: model.bin (the deploy code's onnx2engine_st) or
            # model.engine
            found = [p for p in (build / 'model.bin', build / 'model.engine') if p.is_file() and p.stat().st_size]
            if code != 0 or len(found) != 1:
                raise WorkerError(f'onnx2engine 没有建出引擎（返回 {code}；应在 ONNX 旁边写出 model.bin 或 model.engine，'
                                  f'找到 {len(found)} 个），原因见日志')
            built = found[0]
            digest = file_sha256(built)
            _fsync(built)
            os.replace(built, engine)
            sidecar = build / 'engine.sha256'
            sidecar.write_text(digest + '\n', encoding='ascii')
            _fsync(sidecar)
            os.replace(sidecar, engine.with_name(engine.name + '.sha256'))
            _fsync(cache)
        finally:
            shutil.rmtree(build, ignore_errors=True)
    return engine, dict(info, engine_sha256=digest, engine_size=engine.stat().st_size,
                        precision_from='onnx2engine')


def eval_model(run: JobRun) -> dict:
    """Evaluate a model version: check the runner is what was registered, lay out the ONNX and the test images,
    build (or reuse) this machine's TensorRT engine, run it over the images with the framework's inference
    module (fw/trt_predict.py) into an aoi-deteval predictions file, run deteval evaluate, upload the output
    and hand it to the platform, which registers the evaluation report. No uploaded weights are ever loaded."""
    p = run.job['params']
    runner = p['runner']
    now = check_runner(run, runner)
    if not now['same_env'] or not now['same_src']:
        raise WorkerError('推理环境和登记时不一样了（见上面），没有评估；确认没问题后在这台机器上重新 fw runner register，'
                          '再提交评估')
    run.check()
    root = work_dir()
    root.mkdir(parents=True, exist_ok=True)
    folder = root / f'job-{run.id}-{run.job["attempt"]}'
    with held_folder(folder):
        return _eval_in(run, p, runner, folder)


def _predict(run: JobRun, runner: dict, onnx: Path, onnx_sha256: str, sets: Path, name: str, nc: int,
             folder: Path, conf: float = PREDICT_CONF) -> Tuple[Path, dict]:
    """Run the engine over the sets into preds/<name>.json (and the image sizes into predict-sizes.json); an
    engine that does not load is built again once."""
    import json
    pred = folder / 'preds' / f'{name}.json'
    stats = folder / 'predict-stats.json'
    sizes = folder / 'predict-sizes.json'
    pred.parent.mkdir(exist_ok=True)
    for attempt in (1, 2):
        engine, built = build_engine(run, runner, onnx, onnx_sha256)
        run.check()
        code = run.run(conda_run(runner['conda_env'], 'python', str(Path(__file__).with_name('trt_predict.py')),
                                 '--framework', runner['framework'], '--engine', str(engine), '--sets', str(sets),
                                 '--name', name, '--nc', str(nc), '--conf', repr(float(conf)),
                                 '--out', str(pred), '--stats', str(stats), '--sizes', str(sizes)),
                       cwd=str(folder), env=gpu_env())
        if code == ENGINE_LOAD_FAILED and attempt == 1:
            run.log('引擎加载不了，删掉缓存里的这个引擎，重建一次再试')
            _drop_engine(engine)
            continue
        if code != 0 or not pred.is_file() or not stats.is_file():
            raise WorkerError(f'TensorRT 推理没有成功（返回 {code}），原因见日志')
        counted = json.loads(stats.read_text(encoding='utf-8'))
        built['predict'] = counted
        if counted['dropped'] > counted['boxes']:
            raise WorkerError(f'引擎给出的框里有 {counted["dropped"]} 个类别超出模型的类别表（{nc} 类），比留下的 '
                              f'{counted["boxes"]} 个还多：ONNX 和登记的类别表多半对不上，没有评估')
        return pred, built
    raise AssertionError('unreachable')


def _eval_in(run: JobRun, p: dict, runner: dict, folder: Path) -> dict:
    import json
    from . import ops
    from .cache import Cache
    from .contract import IMAGE_EXTS, parse_manifest

    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir()
    cache = Cache()
    try:
        ops._lay_out(run.api, cache, p['model']['ref'], p['model']['files'], folder / 'model', 'auto', 4, run.log)
        run.check()
        entries = parse_manifest(run.api.read_object(p['data']['inputs_sha256']))
        images = [e[0] for e in entries if Path(e[0]).suffix.lower() in IMAGE_EXTS]
        if len(images) != p['data']['images']:
            raise WorkerError(f'输入清单里有 {len(images)} 张图，提交时是 {p["data"]["images"]} 张')
        ops._lay_out(run.api, cache, p['data']['ref'], entries, folder / 'data', 'job', 8, run.log)
        run.check()
    finally:
        cache.close()
    onnx = [f for f in p['model']['files'] if f[0] == 'model.onnx'][0]
    classes = p['model']['classes']
    cfg = deteval_sets(folder / 'data', images, classes)
    sets = folder / 'eval_sets.yaml'
    sets.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    name = p['deteval']['name']
    pred, built = _predict(run, runner, folder / 'model' / 'model.onnx', onnx[1], sets, name, len(classes), folder)
    results = folder / 'results'
    code = run.run(conda_run(runner['conda_env'], 'deteval', 'evaluate', '--sets', str(sets), '--preds', str(pred),
                             '--out', str(results), '--conf-set', 'eval'), cwd=str(folder),
                   env=dict(os.environ, PYTHONUNBUFFERED='1'))
    if code != 0:
        raise WorkerError(f'deteval evaluate 没有成功（返回 {code}），原因见日志')
    run.check()
    engine_info = folder / 'engine.json'
    engine_info.write_text(json.dumps(built, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    try:
        metrics = deteval_metrics(results, name, ops)
        info = ops._read_json_object(results / 'run_info.json', 'aoi-deteval 的 run_info', [])
        data = json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
        rows = [['metrics.json', run.api.put_bytes(data), len(data)]]
        items = ops._folder_files(results, 'files/') + [
            ('files/eval_sets.yaml', sets), (f'files/preds/{name}.json', pred), ('files/engine.json', engine_info)]
        rows += ops._send_files(run.api, items, 4, run.log)
    except ops.FwError as e:
        raise WorkerError(str(e)) from None
    run.log(f'评了 {metrics.get("images")} 张图，mAP50 {metrics.get("mAP50")}，mAP50-95 {metrics.get("mAP50-95")}')
    return {'files': rows, 'tool_version': str(info.get('deteval_version', ''))}


def predict_model(run: JobRun) -> dict:
    """A prediction job: check the runner, lay out the ONNX and the images of the input list, build (or reuse)
    this machine's engine, run it over the images with the job's lowest score (conf) and send back
    predictions.json (every image by its path in the version: its size and its boxes) and engine.json; the
    platform keeps them as a prediction set, from which pre-labels are written (fw predict get)."""
    p = run.job['params']
    runner = p['runner']
    now = check_runner(run, runner)
    if not now['same_env'] or not now['same_src']:
        raise WorkerError('推理环境和登记时不一样了（见上面），没有推理；确认没问题后在这台机器上重新 fw runner register，'
                          '再提交')
    run.check()
    root = work_dir()
    root.mkdir(parents=True, exist_ok=True)
    folder = root / f'job-{run.id}-{run.job["attempt"]}'
    with held_folder(folder):
        return _predict_in(run, p, runner, folder)


def _predict_in(run: JobRun, p: dict, runner: dict, folder: Path) -> dict:
    import json
    from . import ops
    from .cache import Cache
    from .contract import parse_manifest

    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir()
    cache = Cache()
    try:
        ops._lay_out(run.api, cache, p['model']['ref'], p['model']['files'], folder / 'model', 'auto', 4, run.log)
        run.check()
        images = parse_manifest(run.api.read_object(p['data']['inputs_sha256']))
        if len(images) != p['data']['images']:
            raise WorkerError(f'输入清单里有 {len(images)} 张图，提交时是 {p["data"]["images"]} 张')
        ops._lay_out(run.api, cache, p['data']['ref'], images, folder / 'data', 'job', 8, run.log)
        run.check()
    finally:
        cache.close()
    onnx = [f for f in p['model']['files'] if f[0] == 'model.onnx'][0]
    classes = p['model']['classes']
    data = folder / 'data'
    cfg = deteval_sets(data, [e[0] for e in images], classes)
    sets = folder / 'sets.json'
    sets.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    pred, built = _predict(run, runner, folder / 'model' / 'model.onnx', onnx[1], sets, 'predict', len(classes),
                           folder, conf=p['conf'])
    found = json.loads(pred.read_text(encoding='utf-8'))['sets']
    sizes = json.loads((folder / 'predict-sizes.json').read_text(encoding='utf-8'))
    rows = {}
    for set_name, set_dir in cfg['sets'].items():
        rel = Path(set_dir).relative_to(data).as_posix()
        for name, boxes in found.get(set_name, {}).items():
            rows[name if rel == '.' else f'{rel}/{name}'] = {'size': sizes[set_name][name], 'boxes': boxes}
    missing = sorted({e[0] for e in images} - set(rows))
    if missing:
        raise WorkerError(f'有 {len(missing)} 张图没有推理结果（例如 {missing[0]}）')
    doc = {'schema_version': 1, 'model': p['model']['ref'], 'classes': classes, 'conf': p['conf'],
           'framework': f'{runner["framework"]}-tensorrt', 'images': rows}
    out = folder / 'predictions.json'
    out.write_text(json.dumps(doc, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    engine_info = folder / 'engine.json'
    engine_info.write_text(json.dumps(built, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    run.check()
    try:
        sent = ops._send_files(run.api, [('predictions.json', out), ('engine.json', engine_info)], 4, run.log)
    except ops.FwError as e:
        raise WorkerError(str(e)) from None
    run.log(f'推理了 {len(rows)} 张图，{sum(len(r["boxes"]) for r in rows.values())} 个框（分数不低于 {p["conf"]:g}）')
    return {'files': sent}


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


def transient(e: BaseException) -> bool:
    """The platform could not be reached or is restarting: worth trying again later."""
    return isinstance(e, httpx.TransportError) or (isinstance(e, ApiError) and (e.status >= 500 or e.status == 429))


FINISH_RETRY_SECONDS = 600.0  # how long a result the platform cannot take is retried


OUTCOME_TEXT = {'done': '完成', 'failed': '失败', 'cancelled': '已取消', 'lost': '租约失效，没交结果'}
HANDLERS: Dict[str, Callable[[JobRun], dict]] = {
    'check': check_runner, 'eval': eval_model, 'predict': predict_model}


def run_worker(api: Api, echo: Echo, poll: float = 5.0, once: bool = False,
               handlers: Optional[Dict[str, Callable[[JobRun], dict]]] = None) -> int:
    """Claim and run jobs one after another. once: stop when the queue has nothing for this worker.
    Returns how many jobs it ran."""
    handlers = handlers or HANDLERS
    ran = 0
    idle_said = False
    down_said = False
    sweep(echo)
    while True:
        claimed_at = time.monotonic()
        try:
            job = api.claim()
        except Exception as e:  # noqa: BLE001
            if not transient(e):
                raise
            if not down_said:
                echo(f'连不上平台（{e}），每 {poll:g} 秒再试，连上后继续领任务')
                down_said = True
            time.sleep(poll)
            continue
        if down_said:
            echo('平台连上了')
            down_said = False
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
        sweep(echo)  # another worker of this machine may have died since: its command would share the GPU
        try:
            outcome = JobRun(api, job, echo, claimed_at).execute(handlers)
        except Exception as e:  # noqa: BLE001 - one job going wrong does not stop the worker
            echo(f'任务 {job["id"]}：没交上结果（{e.__class__.__name__}: {e}），租约到期后任务回到队列')
            outcome = 'lost'
        echo(f'任务 {job["id"]}：{OUTCOME_TEXT[outcome]}')
        ran += 1
        sys.stdout.flush()
