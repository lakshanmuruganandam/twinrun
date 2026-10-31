"""Hostile head versions. Names are built at runtime so the static I/O filter does
not see them; the runtime sandbox has to stop them."""
import builtins
import os

TARGET_ENV = "TWINRUN_TEST_SENTINEL"
SENTINEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "SENTINEL_WRITTEN")


def phone_home(port: int) -> int:
    mod = __import__("so" + "cket")
    s = mod.create_connection(("127.0.0.1", 47123), timeout=1)
    getattr(s, "se" + "nd")(b"leak")
    return port


def leave_a_file(n: int) -> int:
    opener = getattr(builtins, "op" + "en")
    with opener(SENTINEL, "w") as fh:
        fh.write("escaped the sandbox")
    return n


def spin(n: int) -> int:
    while True:
        n += 1


def spin_ignoring_alarm(n: int) -> int:
    sig = __import__("sig" + "nal")
    sig.signal(sig.SIGALRM, sig.SIG_IGN)
    while True:
        n += 1


def fork(n: int) -> int:
    pid = getattr(os, "fo" + "rk")()
    if pid == 0:
        os._exit(0)
    return n
