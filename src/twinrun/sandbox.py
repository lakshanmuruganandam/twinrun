"""Parent side of the sandbox: spawn, talk to, and kill worker processes.

Defence in depth:
  1. OS sandbox. On macOS every worker runs under ``sandbox-exec`` with a profile
     that denies all network access and every file write outside its own temp
     dir. On Linux, ``unshare -rn`` (a new user + network namespace) removes the
     network when available.
  2. In-process audit hook (see worker.py) that blocks sockets, subprocesses,
     and writes outside the temp dir, and records what was attempted.
  3. Resource limits (CPU seconds, file size, address space where enforced) and a
     per-call alarm, plus a hard wall-clock deadline enforced from here: a worker
     that does not answer in time is SIGKILLed and restarted.
"""

from __future__ import annotations

import os
import pickle
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

WORKER = Path(__file__).with_name("worker.py")

_PROFILE = """(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath "{tmp}") (literal "/dev/null") (literal "/dev/dtracehelper") (regex #"^/dev/tty"))
"""


_KIND: str | None = None


def sandbox_kind() -> str:
    """Pick the strongest OS sandbox that actually works here (probed once).

    Many Linux hosts (e.g. Ubuntu 24.04 CI runners) forbid unprivileged user
    namespaces, so `unshare -rn` is tried for real before being relied on."""
    global _KIND
    if _KIND is None:
        _KIND = "audit-hook-only"
        if sys.platform == "darwin" and shutil.which("sandbox-exec"):
            _KIND = "sandbox-exec"
        elif sys.platform.startswith("linux") and shutil.which("unshare"):
            try:
                ok = subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            if ok:
                _KIND = "unshare"
    return _KIND


class WorkerDied(Exception):
    pass


@dataclass
class Worker:
    root: str
    module: str
    func: str
    call_timeout: float = 1.0
    proc: subprocess.Popen | None = field(default=None, repr=False)
    tmp: str = ""
    import_error: str | None = None
    calls: int = 0

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="twinrun-w-"))
        cmd = [
            sys.executable, "-I", "-B", str(WORKER),
            "--root", self.root, "--module", self.module, "--func", self.func,
            "--tmp", self.tmp, "--timeout", str(self.call_timeout),
        ]
        kind = sandbox_kind()
        if kind == "sandbox-exec":
            profile = Path(self.tmp) / ".profile.sb"
            profile.write_text(_PROFILE.format(tmp=self.tmp))
            cmd = ["sandbox-exec", "-f", str(profile), *cmd]
        elif kind == "unshare":
            cmd = ["unshare", "-rn", *cmd]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONHASHSEED": "0", "LANG": "C.UTF-8"}
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, cwd=self.tmp, start_new_session=True,
        )
        msg = self._recv(deadline=30.0)
        if msg is None:
            self.import_error = "worker did not start (timeout)"
            self.kill()
        elif msg[0] == "import_error":
            self.import_error = msg[1]
            self.kill()

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self.proc.kill()
            self.proc.wait()
        self.proc = None

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self._send(("quit",))
                self.proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
        self.kill()
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # -------------------------------------------------------------- calls
    def call(self, args: tuple, kwargs: dict) -> dict:
        if self.import_error:
            raise WorkerDied(self.import_error)
        if self.proc is None or self.proc.poll() is not None:
            self.start()
            if self.import_error:
                raise WorkerDied(self.import_error)
        self.calls += 1
        try:
            self._send(("call", tuple(args), dict(kwargs)))
        except (BrokenPipeError, OSError):
            self.kill()
            return {"kind": "exc", "type": "WorkerCrashed", "msg": "worker process died"}
        msg = self._recv(deadline=self.call_timeout + 2.0)
        if msg is None:
            # Hung in C code, or ignored the alarm: hard kill and restart lazily.
            alive = self.proc is not None and self.proc.poll() is None
            self.kill()
            if alive:
                return {"kind": "timeout", "how": f"process killed after {self.call_timeout + 2.0:g}s"}
            return {"kind": "exc", "type": "WorkerCrashed", "msg": "worker process died (resource limit?)"}
        return msg[1]

    def _send(self, obj) -> None:
        assert self.proc and self.proc.stdin
        data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        self.proc.stdin.write(struct.pack(">I", len(data)) + data)
        self.proc.stdin.flush()

    def _read_exact(self, n: int, deadline: float) -> bytes | None:
        import time

        assert self.proc and self.proc.stdout
        fd = self.proc.stdout.fileno()
        buf = b""
        end = time.monotonic() + deadline
        while len(buf) < n:
            left = end - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([fd], [], [], left)
            if not r:
                return None
            chunk = os.read(fd, n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _recv(self, deadline: float):
        head = self._read_exact(4, deadline)
        if head is None:
            return None
        (n,) = struct.unpack(">I", head)
        body = self._read_exact(n, deadline)
        if body is None:
            return None
        return safe_loads(body)


class _NoGlobals(pickle.Unpickler):
    """The code under test shares a process with the worker, so it could write to
    the reply channel. Refusing every global means a reply can only ever contain
    plain containers and scalars, never an object that runs code when loaded."""

    def find_class(self, module, name):
        raise pickle.UnpicklingError(f"refusing global {module}.{name} from worker")


def safe_loads(data: bytes):
    import io

    try:
        return _NoGlobals(io.BytesIO(data)).load()
    except Exception as e:  # noqa: BLE001
        return ("result", {"kind": "exc", "type": "ProtocolError", "msg": f"bad reply from worker: {e}"[:200]})
