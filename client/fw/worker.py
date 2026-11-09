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


def register_runner(api: Api, name: str, env: str, src_dir: str = '', framework: str = '') -> dict:
    """Run on the GPU machine with its worker account: records the environment's package listing (uploaded to
    the store, so a later change can be compared), the source commit and the aoi-deteval framework adapter
    (ultralytics, rfdetr; '' = the runner is not used for evaluations) as the runner's next version."""
    listing = conda_listing(env)
    digest = api.put_bytes(listing)  # the platform makes it durable before it records the runner
    src = source_info(src_dir)
    return api.register_runner({'name': name, 'conda_env': env, 'env_sha256': digest, 'framework': framework,
                                **src})


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
        self.reaper: Optional[Callable[[], None]] = None  # kills what runs under the model account (RunAs)
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
            self._reap()
            _kill(proc)  # what it left in its group (the group lives on while the ended leader is not reaped)
            for r in readers:
                while r.is_alive():
                    r.join(0.2)
                    if self.cancel.is_set() or self.expired():  # Windows: a leftover may still hold the pipe
                        raise LeaseLost() if self.lost.is_set() else Cancelled()
        except BaseException:
            self._reap()
            _kill(proc)
            for r in readers:
                r.join(5)
            raise
        return proc.returncode

    def _reap(self) -> None:
        if self.reaper is not None:
            self.reaper()

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
                job = self.api.finish(self.id, self.token, outcome, result if outcome == 'done' else None,
                                      error if outcome == 'failed' else '')
            except ApiError as e:
                if outcome != 'done' or e.status != 400:
                    raise
                # the platform refused the result (e.g. the report does not add up): the job failed, and why
                outcome = 'failed'
                self.log(f'失败：平台没有收下结果：{e.message}')
                self._send_log()
                job = self.api.finish(self.id, self.token, 'failed', None, f'平台没有收下结果：{e.message}')
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


# --- running model code ---------------------------------------------------------------------------------
# What a model account's commands get from the worker's environment: the GPU choice and the locale. Nothing
# else: no FW_* settings, no proxies with passwords, nothing the worker's own account was given.
RUN_AS_PASS = ('CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER', 'LANG', 'LC_ALL', 'TZ')
RUN_AS_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'


class RunAs:
    """Model code runs under its own low-privilege system account (FW_RUN_AS), through sudo.

    Loading uploaded weights runs code: a .pt / .pth is a pickle, and RF-DETR and YOLO checkpoints cannot be
    loaded with torch's weights_only. So whatever reads them (deteval) runs as an account that cannot read the
    worker's token or files, with an empty environment except RUN_AS_PASS. The worker's account needs one sudo
    rule: `<worker account> ALL=(<model account>) NOPASSWD: /usr/bin/env` (docs/commands.md). One model account
    per worker: the worker kills every process of that account when a command ends or is cancelled (a process
    of another account cannot be signalled by the worker; sudo itself runs as root)."""

    def __init__(self, user: str):
        import pwd
        self.user = user
        try:
            entry = pwd.getpwnam(user)
        except KeyError:
            raise WorkerError(f'FW_RUN_AS={user}：这台机器上没有这个系统账号') from None
        if entry.pw_uid in (0, os.getuid()):
            raise WorkerError(f'FW_RUN_AS={user} 不能是 root 或 worker 自己的账号：模型要在单独的低权限账号下跑')
        self.home = entry.pw_dir
        sudo = shutil.which('sudo')
        if not sudo:
            raise WorkerError('没装 sudo：要用 sudo 切到 FW_RUN_AS 的账号跑模型')
        self.sudo = sudo

    def _sudo(self, argv: List[str]) -> List[str]:
        return [self.sudo, '-n', '-H', '-u', self.user, '--', '/usr/bin/env', '-i', f'HOME={self.home}',
                f'USER={self.user}', f'LOGNAME={self.user}', f'PATH={RUN_AS_PATH}',
                *[f'{k}={os.environ[k]}' for k in RUN_AS_PASS if k in os.environ], *argv]

    def argv(self, argv: List[str], env: Optional[Dict[str, str]] = None) -> List[str]:
        """argv run as the model account; env: more variables (on top of the empty environment). Files it makes
        are readable by the worker (umask 022)."""
        extra = [f'{k}={v}' for k, v in (env or {}).items()]
        return self._sudo([*extra, '/bin/sh', '-c', 'umask 022 && exec "$@"', 'sh', *argv])

    def _plain(self, argv: List[str], timeout: float = 60) -> Tuple[int, str]:
        try:
            r = subprocess.run(self._sudo(argv), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            return 1, str(e)
        return r.returncode, r.stderr.decode('utf-8', 'replace').strip()[-500:]

    def check(self) -> None:
        code, err = self._plain(['true'])
        if code != 0:
            raise WorkerError(f'切不到账号 {self.user}（sudo -n -u {self.user}）：{err or "返回 " + str(code)}；'
                              f'要在 sudoers 里加 {os.environ.get("USER", "<worker 账号>")} ALL=({self.user}) '
                              'NOPASSWD: /usr/bin/env')

    def kill_all(self) -> None:
        """Kill every process of the model account (what a command left running included)."""
        self._plain(['kill', '-KILL', '-1'], 30)

    def remove(self, paths: List[Path]) -> None:
        """Remove what the model account wrote (the worker cannot delete inside its folders)."""
        if paths:
            self._plain(['rm', '-rf', '--', *[str(x) for x in paths]], 300)


def run_as() -> RunAs:
    user = os.environ.get('FW_RUN_AS', '').strip()
    if not user:
        raise WorkerError('这台 worker 没配 FW_RUN_AS（跑模型用的低权限系统账号），不跑会加载模型权重的任务：'
                          '模型权重文件里可以带代码，要和 worker 的令牌隔开；配置方法见 docs/commands.md「fw worker」')
    return RunAs(user)


def _readable(folder: Path) -> None:
    """Let the model account read what the worker laid out (only adds read bits: a laid-out file can be a hard
    link to a read-only cache object)."""
    for d, dirs, files in os.walk(folder):
        os.chmod(d, os.stat(d).st_mode | 0o555)
        for f in files:
            fp = os.path.join(d, f)
            os.chmod(fp, os.stat(fp).st_mode | 0o444)


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
    and hand it to the platform, which registers the evaluation report. deteval (which loads the weights) runs
    under the model account (RunAs)."""
    p = run.job['params']
    runner, deteval = p['runner'], p['deteval']
    model = run_as()
    model.check()
    now = check_runner(run, runner)
    if not now['same_env'] or not now['same_src']:
        raise WorkerError('推理环境和登记时不一样了（见上面），没有评估；确认没问题后在这台机器上重新 fw runner register，'
                          '再提交评估')
    run.check()
    root = work_dir()
    root.mkdir(parents=True, exist_ok=True)
    folder = root / f'job-{run.id}-{run.job["attempt"]}'
    written = [folder / 'preds', folder / 'results', folder / 'tmp']  # what the model account writes
    run.reaper = model.kill_all
    model.kill_all()  # anything a crashed worker left running
    try:
        result = _eval_in(run, p, runner, deteval, model, folder, written)
    finally:
        model.kill_all()
        run.reaper = None
        if not os.environ.get('FW_KEEP_WORK'):
            model.remove([x for x in written if x.exists()])
            shutil.rmtree(folder, ignore_errors=True)
    return result


def _eval_in(run: JobRun, p: dict, runner: dict, deteval: dict, model: RunAs, folder: Path,
             written: List[Path]) -> dict:
    import json
    from . import ops
    from .cache import Cache
    from .contract import IMAGE_EXTS, parse_manifest

    if folder.exists():
        model.remove([x for x in written if x.exists()])
        shutil.rmtree(folder)
    folder.mkdir()
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
    _readable(folder)
    for x in written:
        x.mkdir()
        os.chmod(x, 0o777)  # the model account writes here (and nothing else of the job's folder)
    env = {'PYTHONUNBUFFERED': '1', 'TMPDIR': str(folder / 'tmp')}
    name = deteval['name']
    argv = ['deteval', 'predict', '--sets', str(sets), '--weights', str(folder / 'model' / Path(weights[0]).name),
            '--name', name, '--framework', runner['framework'], '--out', str(folder / 'preds')]
    if runner['src_dir']:
        argv += ['--src', runner['src_dir']]
    if deteval.get('batch'):
        argv += ['--batch', str(deteval['batch'])]
    code = run.run(model.argv(conda_run(runner['conda_env'], *argv), env), cwd=str(folder))
    if code != 0:
        raise WorkerError(f'deteval predict 没有成功（返回 {code}），原因见日志')
    results = folder / 'results'
    code = run.run(model.argv(conda_run(runner['conda_env'], 'deteval', 'evaluate', '--sets', str(sets), '--preds',
                                        str(folder / 'preds' / f'{name}.json'), '--out', str(results),
                                        '--conf-set', 'eval'), env), cwd=str(folder))
    if code != 0:
        raise WorkerError(f'deteval evaluate 没有成功（返回 {code}），原因见日志')
    run.check()
    try:
        metrics = deteval_metrics(results, name, ops)
        info = ops._read_json_object(results / 'run_info.json', 'aoi-deteval 的 run_info', [])
        data = json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
        rows = [['metrics.json', run.api.put_bytes(data), len(data)]]
        items = ops._folder_files(results, 'files/') + [
            ('files/eval_sets.yaml', sets), (f'files/preds/{name}.json', folder / 'preds' / f'{name}.json')]
        rows += ops._send_files(run.api, items, 4, run.log)
    except ops.FwError as e:
        raise WorkerError(str(e)) from None
    run.log(f'评了 {metrics.get("images")} 张图，mAP50 {metrics.get("mAP50")}，mAP50-95 {metrics.get("mAP50-95")}')
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
        claimed_at = time.monotonic()
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
        outcome = JobRun(api, job, echo, claimed_at).execute(handlers)
        echo(f'任务 {job["id"]}：{OUTCOME_TEXT[outcome]}')
        ran += 1
        sys.stdout.flush()
