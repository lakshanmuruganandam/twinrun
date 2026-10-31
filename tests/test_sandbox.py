"""The sandbox must block or kill every hostile behavior, and TwinRun must report it, not crash.

The hostile code in tests/sandbox_fixtures builds names at runtime (``__import__("so"+"cket")``)
so the static I/O filter does not catch it: these tests exercise the runtime sandbox.
We check from the outside too: a real TCP listener must see no connection, and the
sentinel file must not exist afterwards.
"""

import socket
import threading
from pathlib import Path

import pytest

from twinrun.engine import Options, analyze
from twinrun.sandbox import Worker, safe_loads, sandbox_kind

FIX = Path(__file__).parent / "sandbox_fixtures"
SENTINEL = FIX / "SENTINEL_WRITTEN"


@pytest.fixture
def listener():
    """A real TCP server on the port the hostile code dials. Counts accepted connections."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 47123))
    srv.listen()
    srv.settimeout(0.2)
    hits = []
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                hits.append(conn.recv(100))
                conn.close()
            except OSError:
                pass

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    yield hits
    stop.set()
    t.join()
    srv.close()


@pytest.fixture(scope="module")
def report():
    SENTINEL.unlink(missing_ok=True)
    return analyze(FIX / "base", FIX / "head", Options(max_examples=20))


def by_name(report, name):
    return next(f for f in report.functions if f.qualname == name)


def test_network_is_blocked(listener):
    rep = analyze(FIX / "base", FIX / "head", Options(max_examples=5))
    f = by_name(rep, "phone_home")
    assert f.status == "skipped" and "network" in f.reason
    assert listener == [], "hostile code reached the TCP listener"


def test_write_outside_tmp_is_blocked(report):
    f = by_name(report, "leave_a_file")
    assert f.status == "skipped" and "filesystem" in f.reason
    assert not SENTINEL.exists()


def test_fork_is_blocked(report):
    f = by_name(report, "fork")
    assert f.status == "skipped" and "subprocess" in f.reason


def test_infinite_loop_is_interrupted_and_reported(report):
    f = by_name(report, "spin")
    assert f.status == "compared"
    g = f.groups[0]
    assert g.kind == "timeout" and "interrupted" in g.head and g.base == "0"


def test_loop_that_ignores_the_alarm_is_killed(report):
    f = by_name(report, "spin_ignoring_alarm")
    assert f.groups[0].kind == "timeout" and "killed" in f.groups[0].head


def test_os_sandbox_blocks_even_without_audit_hook(tmp_path):
    """Layer 1 alone: talk to the OS sandbox directly with a raw socket syscall path."""
    if sandbox_kind() != "sandbox-exec":
        pytest.skip("macOS sandbox-exec only")
    mod = tmp_path / "raw.py"
    mod.write_text(
        "import _socket\n"
        "def go(x: int):\n"
        "    s = _socket.socket(2, 1)\n"   # AF_INET, SOCK_STREAM, no socket.* audit event
        "    s.connect(('1.1.1.1', 80))\n"
        "    return 'connected'\n")
    with Worker(str(tmp_path), "raw", "go", call_timeout=3) as w:
        out = w.call((1,), {})
    assert out["kind"] in ("exc", "blocked")
    assert out.get("type") in (None, "PermissionError", "SandboxViolation", "OSError")


def test_import_error_is_reported(tmp_path):
    (tmp_path / "bad.py").write_text("import does_not_exist_xyz\ndef f(x: int):\n    return x\n")
    w = Worker(str(tmp_path), "bad", "f")
    w.start()
    assert w.import_error and "does_not_exist_xyz" in w.import_error
    w.close()


def test_reply_channel_refuses_pickled_objects():
    import os
    import pickle

    evil = pickle.dumps(os.system)  # a global reference: would resolve to a callable
    msg = safe_loads(evil)
    assert msg[0] == "result" and msg[1]["type"] == "ProtocolError"
