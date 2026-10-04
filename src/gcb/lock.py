"""Process lock for a single local worker; released by the OS on crash."""
from contextlib import contextmanager
import os

from .core import BridgeError


@contextmanager
def worker_lock(home):
    home.mkdir(parents=True, exist_ok=True)
    f = open(home / "worker.lock", "a+b")
    try:
        f.seek(0)
        f.write(b"0")
        f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise BridgeError("Another worker owns this queue") from exc
        yield
    finally:
        try:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(f.fileno(),fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()
