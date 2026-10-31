"""Sandboxed worker: imports ONE function from ONE checkout and calls it on demand.

This file is run as a standalone script (``python -I -B worker.py ...``) inside a
sandbox, so it must only use the standard library. The parent talks to it over
the inherited stdin/stdout file descriptors using length-prefixed pickles; the
target code's own stdout/stdin are redirected away from that channel.

Protocol (parent -> worker):  ("call", args_tuple, kwargs_dict) | ("quit",)
Protocol (worker -> parent):  ("ready", None) | ("import_error", msg) | ("result", outcome)

An outcome is a dict:
  {"kind": "ok", "value": <canonical tree>, "type": str, "stdout": str, "args_after": tree}
  {"kind": "exc", "type": str, "msg": str, "stdout": str, "args_after": tree}
  {"kind": "timeout"}
  {"kind": "blocked", "what": str}   # code tried something the sandbox forbids
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import importlib
import inspect
import io
import itertools
import math
import os
import pickle
import re
import signal
import struct
import sys
import types

MAX_ITEMS = 200
MAX_STR = 2000
MAX_DEPTH = 12
_ADDR = re.compile(r"0x[0-9a-fA-F]{4,}")


class _Timeout(BaseException):
    """BaseException so that user code's ``except Exception`` cannot swallow it."""


class SandboxViolation(PermissionError):
    pass


# ---------------------------------------------------------------- canonical form

def canonical(v, depth: int = 0):
    """Turn any value into a comparable, picklable tree of builtins.

    Order-insensitive containers are sorted, memory addresses are stripped, and
    floats are kept as floats so the parent can compare them with a tolerance.
    """
    if depth > MAX_DEPTH:
        return ("deep", None)
    t = type(v)
    if v is None or t is bool or t is int:
        return (t.__name__, v)
    if t is float:
        if math.isnan(v):
            return ("float", "nan")
        return ("float", v)
    if t is complex:
        return ("complex", (v.real, v.imag))
    if t is str:
        return ("str", v[:MAX_STR])
    if t in (bytes, bytearray):
        return (t.__name__, bytes(v[:MAX_STR]))
    if t in (list, tuple):
        return (t.__name__, [canonical(x, depth + 1) for x in v[:MAX_ITEMS]])
    if t in (set, frozenset):
        items = [canonical(x, depth + 1) for x in itertools.islice(v, MAX_ITEMS)]
        return (t.__name__, sorted(items, key=repr))
    if isinstance(v, dict):
        items = [
            (canonical(k, depth + 1), canonical(x, depth + 1))
            for k, x in itertools.islice(v.items(), MAX_ITEMS)
        ]
        return ("dict", sorted(items, key=repr))
    if isinstance(v, (types.GeneratorType, range, map, filter, zip, enumerate, reversed)):
        return ("iter", [canonical(x, depth + 1) for x in itertools.islice(v, MAX_ITEMS)])
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        fields = {f.name: getattr(v, f.name, None) for f in dataclasses.fields(v)}
        return ("obj", t.__qualname__, canonical(fields, depth + 1))
    if isinstance(v, (int, float, str)):  # enums, int/str subclasses
        return (t.__qualname__, _ADDR.sub("0x?", repr(v))[:MAX_STR])
    d = getattr(v, "__dict__", None)
    if isinstance(d, dict) and not isinstance(v, (type, types.FunctionType, types.ModuleType)):
        return ("obj", t.__qualname__, canonical(d, depth + 1))
    return ("repr", t.__qualname__, _ADDR.sub("0x?", repr(v))[:MAX_STR])


# ---------------------------------------------------------------- sandbox

_violations: list[str] = []
_allowed_write_root = ""
_in_call = False

_BLOCK_EVENTS = {
    "socket.connect": "network",
    "socket.bind": "network",
    "socket.getaddrinfo": "network",
    "socket.gethostbyname": "network",
    "socket.sendto": "network",
    "os.system": "subprocess",
    "subprocess.Popen": "subprocess",
    "os.fork": "subprocess",
    "os.forkpty": "subprocess",
    "os.exec": "subprocess",
    "os.posix_spawn": "subprocess",
    "os.spawn": "subprocess",
    "os.kill": "signal",
    "ctypes.dlopen": "ctypes",
    "os.remove": "filesystem",
    "os.rename": "filesystem",
    "os.rmdir": "filesystem",
    "os.mkdir": "filesystem",
    "shutil.rmtree": "filesystem",
}


def _write_target_ok(path) -> bool:
    try:
        p = os.path.realpath(os.fsdecode(path))
    except Exception:
        return False
    return p == "/dev/null" or p.startswith(_allowed_write_root + os.sep)


def _audit(event: str, args) -> None:
    what = _BLOCK_EVENTS.get(event)
    if what == "filesystem":
        if args and _write_target_ok(args[0]):
            return
    elif event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        writing = False
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            writing = True
        if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND):
            writing = True
        if not writing or path is None or isinstance(path, int) or _write_target_ok(path):
            return
        what = "filesystem"
    elif what is None:
        return
    msg = f"{what}: {event} {args[0] if args else ''}".strip()[:200]
    _violations.append(msg)
    raise SandboxViolation(f"blocked by TwinRun sandbox ({msg})")


def _limits() -> None:
    try:
        import resource

        for name, val in (("RLIMIT_CPU", 120), ("RLIMIT_FSIZE", 16 << 20), ("RLIMIT_CORE", 0)):
            lim = getattr(resource, name, None)
            if lim is not None:
                with contextlib.suppress(Exception):
                    resource.setrlimit(lim, (val, val))
        lim = getattr(resource, "RLIMIT_AS", None)
        if lim is not None:
            with contextlib.suppress(Exception):  # not enforced on macOS; works on Linux
                resource.setrlimit(lim, (1 << 31, 1 << 31))
    except ImportError:
        pass


# ---------------------------------------------------------------- protocol

def _send(fp, obj) -> None:
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    fp.write(struct.pack(">I", len(data)) + data)
    fp.flush()


def _recv(fp):
    head = fp.read(4)
    if len(head) < 4:
        return ("quit",)
    (n,) = struct.unpack(">I", head)
    return pickle.loads(fp.read(n))


def _resolve(module_name: str, qualname: str):
    mod = importlib.import_module(module_name)
    obj = mod
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _call(fn, args, kwargs, timeout: float) -> dict:
    global _in_call
    out = io.StringIO()
    _violations.clear()

    def on_alarm(signum, frame):
        raise _Timeout()

    signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        with contextlib.redirect_stdout(out):
            _in_call = True
            value = fn(*args, **kwargs)
            if inspect.isawaitable(value):
                value = asyncio.run(_awaited(value))
            tree = canonical(value)
        result = {"kind": "ok", "value": tree, "type": type(value).__name__}
    except _Timeout:
        return {"kind": "timeout"}
    except RecursionError:
        result = {"kind": "exc", "type": "RecursionError", "msg": "maximum recursion depth exceeded"}
    except BaseException as e:  # noqa: BLE001 - we report every exception
        if isinstance(e, (KeyboardInterrupt, SystemExit)) and not _in_call:
            raise
        result = {"kind": "exc", "type": type(e).__qualname__, "msg": _ADDR.sub("0x?", str(e))[:500]}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        _in_call = False
    if _violations:
        return {"kind": "blocked", "what": _violations[0]}
    result["stdout"] = out.getvalue()[:MAX_STR]
    try:
        result["args_after"] = canonical((list(args), kwargs))
    except Exception:  # noqa: BLE001
        result["args_after"] = None
    return result


async def _awaited(aw):
    return await aw


def main() -> None:
    global _allowed_write_root
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--module", required=True)
    ap.add_argument("--func", required=True)
    ap.add_argument("--tmp", required=True)
    ap.add_argument("--timeout", type=float, default=1.0)
    ns = ap.parse_args()

    # Private protocol channel; the target code gets /dev/null for fds 0 and 1.
    proto_in = os.fdopen(os.dup(0), "rb")
    proto_out = os.fdopen(os.dup(1), "wb")
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    sys.stdin = open(os.devnull)  # noqa: SIM115

    _allowed_write_root = os.path.realpath(ns.tmp)
    os.chdir(_allowed_write_root)
    os.environ["HOME"] = _allowed_write_root
    os.environ["TMPDIR"] = _allowed_write_root
    sys.path.insert(0, os.path.realpath(ns.root))
    sys.dont_write_bytecode = True
    sys.setrecursionlimit(3000)
    _limits()
    sys.addaudithook(_audit)

    try:
        fn = _resolve(ns.module, ns.func)
    except BaseException as e:  # noqa: BLE001
        _send(proto_out, ("import_error", f"{type(e).__name__}: {e}"[:500]))
        return
    _send(proto_out, ("ready", None))
    while True:
        msg = _recv(proto_in)
        if msg[0] != "call":
            return
        _, args, kwargs = msg
        _send(proto_out, ("result", _call(fn, args, kwargs, ns.timeout)))


if __name__ == "__main__":
    main()
