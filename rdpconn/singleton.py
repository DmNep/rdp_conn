"""Локальная защита от второй копии одной роли на этой машине.

Не протокол и не буфер обмена: конфликт, который это закрывает, — два
процесса на одном Windows-хосте пишут в одну историю ячеек под одним
peer_id. Ровно такой уже найден живьём для rx (AGENTS.md 3: два `rx` с
одним `--peer-id` дают лавину подтверждений на каждый кадр) и для guest
(`claim_guest()` ниже в этом же файле решал его отдельно, до того, как
здесь появился общий примитив).

Именованный мьютекс Windows, не PID-файл: ОС сама закрывает все хендлы
процесса при его завершении, даже аварийном, и объект исчезает сам —
отличать «жива ли прежняя копия» отдельной проверкой процесса не нужно,
CreateMutexW()+GetLastError() отвечают сразу (ровно это свойство уже
использовал `claim_guest()`, здесь оно обобщено на tx/rx/gate/coder
host/fileshake).

Политика выбрана осознанно разной для двух случаев, которые выглядят
похоже, но не совпадают:

- `claim_guest()` (coder.py) — кооперативная пересдача: Cline вправо
  перезапускать guest-процесс на каждый сеанс, и секунда-две, пока прежний
  ещё закрывает хендл, — обычное дело, не конфликт оператора. Поэтому
  там короткий retry (20 попыток по 0.25 с), а неудача — тихий возврат
  "кодер уже в сессии", не отказ запуска.
- `acquire()` здесь — решение оператора: tx/rx/gate/coder-host/fileshake
  запускает человек, и тихое вытеснение рискует оборвать чужую передачу
  посередине без единого следа (докладная 27.11.2026 про буфер хоста A).
  Поэтому по умолчанию — немедленный отказ с PID и временем старта
  прежней копии, а вытеснение — только явным `--replace`.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import time
from ctypes import wintypes

# GetLastError() после CreateMutexW: объект уже существовал (и значит, у
# него как минимум один живой владелец) -- ровно то число, на котором уже
# держался claim_guest() ниже.
ERROR_ALREADY_EXISTS = 183
_PROCESS_TERMINATE = 0x0001


class SingletonError(Exception):
    """Уже есть живая копия той же роли (и peer_id) на этой машине."""


_INFO_DIR = os.path.join(tempfile.gettempdir(), "rdpconn-locks")


def _safe(label: str) -> str:
    """Имя файла/мьютекса не должно ломаться на peer_id с чем угодно внутри."""
    out = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)
    return out or "_"


def _mutex_name(role: str, peer_id: str | None) -> str:
    # peer_id бывает не задан (см. app.peer_hint) -- и тогда это тоже своя,
    # отдельная от любого реального id, метка, а не "без мьютекса".
    label = peer_id if peer_id else "\x00no-peer-id"
    return f"Local\\rdpconn-{_safe(role)}-{_safe(label)}"


def _info_path(role: str, peer_id: str | None) -> str:
    label = peer_id if peer_id else "no-peer-id"
    return os.path.join(_INFO_DIR, f"{_safe(role)}-{_safe(label)}.json")


_kernel32 = None


def _k32():
    """Кэшированный kernel32 с нужными сигнатурами (use_last_error — иначе
    ctypes.get_last_error() не видит то, что увидел CreateMutexW)."""
    global _kernel32
    if _kernel32 is not None:
        return _kernel32
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.TerminateProcess.restype = wintypes.BOOL
    _kernel32 = k
    return k


def try_claim(name: str) -> int | None:
    """Один захват именованного мьютекса. None — он уже у живого владельца.

    Логика (и код 183) — та же, что уже была в claim_guest(): любой исход
    CreateMutexW кроме ERROR_ALREADY_EXISTS значит, что мьютекс наш — в том
    числе и случай, когда прежний владелец умер и ОС уже освободила объект.
    """
    if sys.platform != "win32":
        return -1
    k = _k32()
    ctypes.set_last_error(0)
    handle = k.CreateMutexW(None, False, name)
    if not handle:
        return -1
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        k.CloseHandle(handle)
        return None
    return handle


def release(handle: int | None) -> None:
    if handle is None or handle == -1:
        return
    _k32().CloseHandle(handle)


def _write_info(role: str, peer_id: str | None) -> None:
    try:
        os.makedirs(_INFO_DIR, exist_ok=True)
        info = {"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        with open(_info_path(role, peer_id), "w", encoding="utf-8") as f:
            json.dump(info, f)
    except OSError:
        pass  # диагностика для сообщения об отказе, не часть самой защиты


def _read_info(role: str, peer_id: str | None) -> dict | None:
    try:
        with open(_info_path(role, peer_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _terminate(pid: int) -> bool:
    k = _k32()
    handle = k.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(k.TerminateProcess(handle, 1))
    finally:
        k.CloseHandle(handle)


class Lock:
    """Захваченный мьютекс. close() (или `with`) отпускает его явно,
    но и без этого завершение процесса отпустит его само — на этом
    свойстве и держится вся защита (см. модульный docstring)."""

    def __init__(self, handle: int | None, role: str, peer_id: str | None) -> None:
        self._handle = handle
        self.role = role
        self.peer_id = peer_id

    def close(self) -> None:
        if self._handle is None:
            return
        release(self._handle)
        self._handle = None

    def __enter__(self) -> "Lock":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def acquire(role: str, peer_id: str | None, *, replace: bool = False) -> Lock:
    """Захватить (role, peer_id) на этой машине.

    Без --replace конфликт с живым владельцем — отказ (SingletonError),
    не тихое вытеснение: см. "Политика" в модульном docstring. С
    --replace прежняя копия завершается (TerminateProcess по PID из
    info-файла) и захват повторяется.
    """
    name = _mutex_name(role, peer_id)
    handle = try_claim(name)
    if handle is None:
        if not replace:
            info = _read_info(role, peer_id)
            detail = (f"PID {info['pid']}, запущен {info['started']}"
                      if info else "подробности прежней копии неизвестны")
            raise SingletonError(
                f"{role} с peer-id={peer_id!r} уже поднят на этой машине ({detail}). "
                f"Это отказ запуска, а не тихий обрыв прежней сессии — если она "
                f"точно мёртва, запустите с --replace"
            )
        info = _read_info(role, peer_id)
        if info is not None and _terminate(int(info["pid"])):
            time.sleep(0.3)  # дать ОС закрыть хендлы завершённого процесса
        handle = try_claim(name)
        if handle is None:
            raise SingletonError(
                f"не удалось вытеснить прежнюю копию {role} (peer-id={peer_id!r})"
            )
    _write_info(role, peer_id)
    return Lock(handle, role, peer_id)


# --- guest: кооперативная пересдача, не отказ оператора ----------------------

_GUEST_MUTEX = None


def claim_guest() -> bool:
    """В сессии один писатель буфера. Второй ждёт, пока предыдущий процесс
    отпустит слот -- см. "Политика" в модульном docstring: это не решение
    оператора, а штатный перезапуск гуеста Cline, тихий отказ не ошибка."""
    if sys.platform != "win32":
        return True
    global _GUEST_MUTEX
    name = "Local\\rdpconn-guest"
    for _ in range(20):
        handle = try_claim(name)
        if handle is not None:
            _GUEST_MUTEX = handle
            return True
        time.sleep(0.25)
    return False


def watch_parent() -> None:
    """Родитель Cline умер, а гость ещё ждёт кадр и держит слот. Тогда выходим."""
    if sys.platform != "win32":
        return
    import threading

    def run() -> None:
        k = _k32()
        ntdll = ctypes.WinDLL("ntdll")

        class INFO(ctypes.Structure):
            _fields_ = [
                ("ExitStatus", ctypes.c_long),
                ("PebBaseAddress", ctypes.c_void_p),
                ("AffinityMask", ctypes.c_size_t),
                ("BasePriority", ctypes.c_long),
                ("UniqueProcessId", ctypes.c_void_p),
                ("InheritedFromUniqueProcessId", ctypes.c_void_p),
            ]

        info = INFO()
        status = ntdll.NtQueryInformationProcess(
            k.GetCurrentProcess(), 0, ctypes.byref(info), ctypes.sizeof(info), None
        )
        if status != 0:
            return
        parent = ctypes.cast(info.InheritedFromUniqueProcessId, ctypes.c_void_p).value or 0
        if parent <= 4:
            return
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = k.OpenProcess(0x00100000, False, parent)
        if not handle:
            os._exit(0)
        k.WaitForSingleObject(handle, 0xFFFFFFFF)
        os._exit(0)

    threading.Thread(target=run, name="rdpconn-parent", daemon=True).start()
