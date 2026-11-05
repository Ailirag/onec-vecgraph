"""Opt-in commit checks and per-workspace reader/writer locks for Git refresh.

Режим включается `ONEC_LITE_REQUIRE_FRESH_INDEX=true`: поиск по FTS отвечает только
индексом, собранным ровно на текущих коммитах источников, а внешний рефреш (обновление
рабочей копии из Git + пересборка индекса) берёт эксклюзивный лок на время публикации
файлов. Читатели берут разделяемый лок без ожидания: занят — значит «идёт обновление».

Локи межпроцессные: на POSIX — `fcntl.flock`, на Windows — `LockFileEx` (те же
семантики shared/exclusive). Файлы локов — `ONEC_LITE_REFRESH_LOCKS`
(по умолчанию `~/.onec-lite/refresh-locks`), имя — хэш корня рабочей копии.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sys
from typing import IO, Iterator


def required() -> bool:
    return os.environ.get("ONEC_LITE_REQUIRE_FRESH_INDEX", "").lower() in ("1", "true", "yes")


def lock_name(root) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24] + ".lock"


def lock_dir() -> Path:
    return Path(os.environ.get("ONEC_LITE_REFRESH_LOCKS",
                               str(Path.home() / ".onec-lite" / "refresh-locks")))


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _k32.LockFileEx.restype = wintypes.BOOL
    _k32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _k32.UnlockFileEx.restype = wintypes.BOOL
    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2

    def _try_lock(handle: IO, *, exclusive: bool, block: bool) -> bool:
        flags = (_LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0) | (0 if block else _LOCKFILE_FAIL_IMMEDIATELY)
        ov = _Overlapped()
        return bool(_k32.LockFileEx(msvcrt.get_osfhandle(handle.fileno()), flags, 0, 1, 0,
                                    ctypes.byref(ov)))

    def _unlock(handle: IO) -> None:
        ov = _Overlapped()
        _k32.UnlockFileEx(msvcrt.get_osfhandle(handle.fileno()), 0, 1, 0, ctypes.byref(ov))
else:
    import fcntl

    def _try_lock(handle: IO, *, exclusive: bool, block: bool) -> bool:
        flags = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if block else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError:
            return False
        return True

    def _unlock(handle: IO) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _lock(root, *, exclusive: bool, block: bool) -> Iterator[bool]:
    directory = lock_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / lock_name(root)).open("a") as handle:
        if not _try_lock(handle, exclusive=exclusive, block=block):
            yield False
            return
        try:
            yield True
        finally:
            _unlock(handle)


@contextmanager
def read_lock(root) -> Iterator[bool]:
    """A refresh never replaces source files while a guarded search is reading.

    Не ждёт: yields False, если рефреш держит эксклюзивный лок."""
    with _lock(root, exclusive=False, block=False) as acquired:
        yield acquired


@contextmanager
def write_lock(root, *, block: bool = True) -> Iterator[bool]:
    """Эксклюзивный лок рефреша: на время публикации новых файлов источника."""
    with _lock(root, exclusive=True, block=block) as acquired:
        yield acquired
