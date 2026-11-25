"""Проверка, возит ли этот RDP файлы через диск сессии.

Один файл, два запуска. Канал шлюза отсюда не строится: редирект диска
отложен, см. docs/protocol.md, раздел 12.

На компьютере, где запущен mstsc:

    py rdp_disk_probe.py host C:\rdp-probe

В файле подключения должен быть перенаправлен диск с этой папкой.
Внутри удалённой сессии:

    py rdp_disk_probe.py guest

Скрипт ищет \\tsclient. Если его нет, редирект диска выключен и без своей
библиотеки mstsc остаётся буфер обмена. Если есть, гость пишет файл, хост
отвечает другим файлом. Так видно, ходят ли байты в обе стороны.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import time
import uuid

READY = "ready.txt"
GUEST = "guest.txt"
HOST = "host.txt"
SKIP = {
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    "$Recycle.Bin",
    "System Volume Information",
    "Recovery",
    "AppData",
}
_REPARSE = 0x400


def say(text: str) -> None:
    print(text, flush=True)


def listing(path: str, seconds: float):
    """Имена в каталоге. None — каталог не ответил за отведённое время."""
    box: dict = {}

    def run() -> None:
        try:
            box["ok"] = os.listdir(path)
        except OSError as exc:
            box["err"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        return None
    if "err" in box:
        raise box["err"]
    return box.get("ok", [])


def is_reparse(path: str) -> bool:
    if os.name != "nt":
        return os.path.islink(path)
    try:
        attrs = ctypes_attributes(path)
    except OSError:
        return False
    if attrs is None or attrs == 0xFFFFFFFF:
        return False
    return bool(attrs & _REPARSE)


def ctypes_attributes(path: str):
    import ctypes
    return ctypes.windll.kernel32.GetFileAttributesW(str(path))


def find_ready(root: str, budget: float):
    """Папка с ready.txt. Глубоко диск не обходим: чужой C: по RDP не читаем."""
    deadline = time.monotonic() + budget
    queue = [(root, 0)]
    seen = 0
    while queue:
        if time.monotonic() > deadline:
            return None
        path, depth = queue.pop(0)
        try:
            names = listing(path, 5)
        except OSError:
            continue
        if names is None:
            continue
        if READY in names:
            return path
        if depth >= 4:
            continue
        for name in names:
            if name in SKIP or name.startswith("."):
                continue
            child = os.path.join(path, name)
            if is_reparse(child):
                continue
            try:
                if not os.path.isdir(child):
                    continue
            except OSError:
                continue
            seen += 1
            if seen > 400:
                return None
            queue.append((child, depth + 1))
    return None


def read_token(path: str) -> str:
    try:
        with open(path, "r", encoding="ascii") as handle:
            return handle.read(80).strip()
    except OSError:
        return ""


def write_token(path: str, token: str) -> None:
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write(token + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def remote_hint(folder: str) -> str:
    full = os.path.abspath(folder)
    drive, tail = os.path.splitdrive(full)
    letter = drive[:1]
    if not letter:
        return full
    tail = tail.replace("/", "\\")
    return "\\\\tsclient\\" + letter + tail


def host_mode(folder: str, wait: float) -> int:
    os.makedirs(folder, exist_ok=True)
    token = uuid.uuid4().hex
    for name in (GUEST, HOST):
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass
    write_token(os.path.join(folder, READY), token)
    try:
        shutil.copy2(os.path.abspath(__file__), os.path.join(folder, "rdp_disk_probe.py"))
    except OSError as exc:
        say(f".. копию скрипта в папку не положил: {exc}")
    remote = remote_hint(folder)
    say(f"папка: {os.path.abspath(folder)}")
    say(f"в сессии она будет примерно здесь: {remote}")
    say("В mstsc перенаправьте диск, на котором лежит эта папка, и подключитесь.")
    say("В сессии, если файла ещё нет на рабочем столе:")
    say(f"  py {remote}\\rdp_disk_probe.py guest")
    say(f"жду файл гостя {wait:.0f} с")
    deadline = time.monotonic() + wait
    guest_path = os.path.join(folder, GUEST)
    while time.monotonic() < deadline:
        if os.path.isfile(guest_path) and read_token(guest_path) == token:
            write_token(os.path.join(folder, HOST), token)
            say("ДИСК: ДА, файл гостя дошёл, ответ записан")
            return 0
        time.sleep(0.4)
    say("ДИСК: НЕТ, гость ничего не записал. Редирект выключен или guest не запущен")
    return 2


def guest_mode(root: str, wait: float) -> int:
    say(f"корень: {root}")
    try:
        names = listing(root, 8)
    except OSError as exc:
        say(f"ДИСК: НЕТ, {root} не открылся ({exc}). Буфер остаётся единственным каналом")
        return 2
    if names is None:
        say(f"ДИСК: НЕТ, {root} молчит. Буфер остаётся единственным каналом")
        return 2
    if not names and not os.path.isdir(root):
        say(f"ДИСК: НЕТ, {root} нет. Буфер остаётся единственным каналом")
        return 2
    say("шары: " + ", ".join(names[:12]))
    folder = find_ready(root, wait)
    if folder is None:
        say("ДИСК: ВИДЕН, но маркер host не найден. Запустите host и перенаправьте его диск")
        return 3
    token = read_token(os.path.join(folder, READY))
    if not token:
        say("ДИСК: ВИДЕН, маркер пустой")
        return 3
    say(f"маркер: {folder}")
    try:
        write_token(os.path.join(folder, GUEST), token)
    except OSError as exc:
        say(f"ДИСК: ВИДЕН, записать файл не вышло ({exc})")
        return 3
    deadline = time.monotonic() + wait
    reply = os.path.join(folder, HOST)
    while time.monotonic() < deadline:
        if os.path.isfile(reply) and read_token(reply) == token:
            say("ДИСК: ДА, обе стороны. Файл ушёл на компьютер с mstsc и ответ вернулся")
            return 0
        time.sleep(0.4)
    say("ДИСК: ВИДЕН, запись ушла, ответ хоста не вернулся")
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rdp_disk_probe")
    sub = parser.add_subparsers(dest="mode", required=True)
    host = sub.add_parser("host", help="компьютер, где запущен mstsc")
    host.add_argument("folder", help="пустая папка, чей диск перенаправлен в сессию")
    host.add_argument("--wait", type=float, default=180)
    guest = sub.add_parser("guest", help="внутри удалённой сессии")
    guest.add_argument("--root", default=r"\\tsclient")
    guest.add_argument("--wait", type=float, default=30)
    args = parser.parse_args(argv)
    if args.mode == "host":
        return host_mode(args.folder, args.wait)
    return guest_mode(args.root, args.wait)


if __name__ == "__main__":
    sys.exit(main())
