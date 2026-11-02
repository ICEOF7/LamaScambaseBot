"""Local single-instance guard for processes using the same SQLite database.

The lock file is intentionally kept after shutdown: removing it could let a
second process lock a different inode while an older process still owns it.
"""
from __future__ import annotations
import os
from pathlib import Path


class InstanceLock:
    def __init__(self, db_path: str):
        database = None if db_path == ':memory:' else Path(db_path).expanduser().resolve()
        self.path = None if database is None else database.with_name(database.name + '.instance.lock')
        self.fd: int | None = None

    def __enter__(self):
        if self.fd is not None:
            raise RuntimeError('InstanceLock is already acquired')
        if self.path is None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(str(self.path), flags, 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b' ')
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError('Эта база уже используется другим процессом бота или сверки ID. '
                               'Сначала останови старый процесс.') from exc
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, str(os.getpid()).encode('ascii'))
            os.ftruncate(fd, os.lseek(fd, 0, os.SEEK_CUR))
        except OSError:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
