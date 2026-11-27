"""Крупный кусок через gate уходит файлом, не ячейками (docs/roadmap.md, п.5).

MemoryClipboard несёт настоящий текстовый обмен (offer/ack/abort) — та же
dial_loop/listen_loop, что и в test_gate.py. Настоящий Windows-буфер обмена
для файла здесь не нужен и не трогается: fileclip.send_files/wait_for_new_file/
list_clipboard_files/recv_file/sequence_number подменены на простую имитацию
поверх временной папки на диске (сам файл реальный, обмен владением буфера —
нет). Это сознательно: переключение режима буфера — то, что OleSetClipboard
стирает текст целиком, см. комментарий в gate.py перед _bulk_round() — здесь
не проверяется никак, его может подтвердить только живой стенд.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rdpconn import clipboard as cb
from rdpconn import fileclip
from rdpconn import gate

_failed = 0


def check(name, ok, detail="") -> None:
    global _failed
    if ok:
        print(f"  {name}: OK  {detail}".rstrip())
    else:
        _failed += 1
        print(f"  {name}: FAIL  {detail}".rstrip())


class FakeFileClip:
    """Подмена fileclip.* на время теста: файл реально лежит на диске
    (временная папка), но без настоящего Windows COM/clipboard — см.
    модульный докстринг про то, чего это НЕ проверяет."""

    def __init__(self, *, fail_send=False, fail_recv=False, corrupt=False,
                never_arrive=False):
        self.fail_send = fail_send
        self.fail_recv = fail_recv
        self.corrupt = corrupt
        self.never_arrive = never_arrive
        self._seq = 0
        self._entries: list[tuple[str, str]] | None = None
        self.send_calls = 0
        self.recv_calls = 0

    def send_files(self, paths):
        self.send_calls += 1
        if self.fail_send:
            raise fileclip.ClipboardFileError("симуляция отказа отправки")
        self._entries = [(os.path.basename(p), p) for p in paths]
        self._seq += 1

    def sequence_number(self):
        return self._seq

    def wait_for_new_file(self, since_seq, *, timeout=120.0, poll_interval=0.05):
        if self.never_arrive or self._entries is None or self._seq == since_seq:
            raise TimeoutError("файл не появился (симуляция)")
        return self._seq

    def list_clipboard_files(self, **kw):
        if self.fail_recv or self._entries is None:
            raise fileclip.ClipboardFileError("симуляция отказа списка файлов")
        return [fileclip.FileEntry(name=n, size=os.path.getsize(p)) for n, p in self._entries]

    def recv_file(self, index, out_path, **kw):
        self.recv_calls += 1
        if self.fail_recv or self._entries is None:
            raise fileclip.ClipboardFileError("симуляция отказа приёма")
        name, path = self._entries[index]
        data = Path(path).read_bytes()
        if self.corrupt:
            data = data[:-1] + bytes([data[-1] ^ 0xFF])
        Path(out_path).write_bytes(data)
        return fileclip.RecvResult(bytes_total=len(data), first_byte_s=0.001, total_s=0.001)


def patch_fileclip(fake: FakeFileClip):
    """Подменить модульные функции fileclip, которые видит gate.py (gate.py
    зовёт их как fileclip.send_files(...) и т.п. — правка атрибутов самого
    модуля действует везде, где он так импортирован)."""
    names = ["send_files", "sequence_number", "wait_for_new_file",
            "list_clipboard_files", "recv_file"]
    saved = {name: getattr(fileclip, name) for name in names}
    for name in names:
        setattr(fileclip, name, getattr(fake, name))
    return saved


def unpatch_fileclip(saved: dict) -> None:
    for name, fn in saved.items():
        setattr(fileclip, name, fn)


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def start_body_server(body: bytes) -> int:
    """HTTP-сервер, отвечающий ровно этим телом (для проверки байт-в-байт
    через gate, с bulk-режимом и без)."""
    port = free_port()
    ready = threading.Event()

    def run() -> None:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.settimeout(20)
        ready.set()
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            srv.close()
            return
        conn.settimeout(15)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            header = (f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n"
                      f"Connection: close\r\n\r\n").encode()
            conn.sendall(header + body)
        except OSError:
            pass
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(2)
    return port


def start_gate(wait: float = 0.3):
    cb.MemoryClipboard.reset()
    dial = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
    listen = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
    stop = threading.Event()
    proxy = free_port()
    threads = [
        threading.Thread(target=gate.dial_loop, args=(dial, stop, wait), daemon=True),
        threading.Thread(target=gate.listen_loop,
                         args=(listen, "127.0.0.1", proxy, stop, wait), daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(0.15)
    return stop, proxy, dial, listen, threads


def stop_gate(stop, dial, listen, threads) -> None:
    stop.set()
    for t in threads:
        t.join(5)
    dial.close()
    listen.close()


def read_body(sock: socket.socket, expect: int, cap: float = 30.0) -> bytes:
    sock.settimeout(cap)
    buf = b""
    deadline = time.monotonic() + cap
    while b"\r\n\r\n" not in buf and time.monotonic() < deadline:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    body = bytearray(rest)
    while len(body) < expect and time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        body.extend(chunk)
    return bytes(body)


def fetch_through_gate(proxy: int, origin: int, expect_len: int) -> bytes:
    client = socket.create_connection(("127.0.0.1", proxy), timeout=5)
    client.sendall(
        f"GET http://127.0.0.1:{origin}/ HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Connection: close\r\n\r\n".encode()
    )
    body = read_body(client, expect_len)
    client.close()
    return body


def main() -> int:
    print("=== 0. BULK_THRESHOLD строго ниже _WINDOW (реальные дефолты, без подмен) ===")
    # Регресс к 28.11.2026: с BULK_THRESHOLD=128КБ, _WINDOW=64КБ to_peer не
    # растёт выше ~_WINDOW+8192, и кандидат в _bulk_round() никогда не
    # набирался -- файловый режим был недостижим при любом объёме тела
    # ответа. Найдено живым прогоном G6, не домыслено (память
    # gate_bulk_window_threshold_bug.md). gate.py теперь падает assert'ом
    # при импорте на то же самое -- этот сценарий проверяет ту же
    # инвариант явным тестом, не полагаясь только на побочный эффект assert.
    check("BULK_THRESHOLD < _WINDOW на реальных константах модуля",
          gate.BULK_THRESHOLD < gate._WINDOW,
          f"BULK_THRESHOLD={gate.BULK_THRESHOLD} _WINDOW={gate._WINDOW}")

    print("=== 1. ниже порога -- обычные ячейки, fileclip не вызывается ===")
    fake = FakeFileClip()
    saved = patch_fileclip(fake)
    try:
        small = os.urandom(4096)
        origin = start_body_server(small)
        stop, proxy, dial, listen, threads = start_gate()
        try:
            got = fetch_through_gate(proxy, origin, len(small))
            check("тело совпало байт-в-байт", got == small, f"{len(got)}/{len(small)}")
            check("fileclip.send_files не звался", fake.send_calls == 0, str(fake.send_calls))
        finally:
            stop_gate(stop, dial, listen, threads)
    finally:
        unpatch_fileclip(saved)

    # Пп. 2-4: порог временно занижен. На живом RDP-лаге (13-26 КБ/с) любой
    # ответ крупнее пары ячеек надёжно держит to_peer выше настоящего порога
    # (131072) долю секунды-секунды — достаточно, чтобы _bulk_round успел
    # сработать. На быстром MemoryClipboard-стенде (без лага вовсе) обычный
    # ACK обрезает to_peer почти мгновенно, и настоящий порог тут попросту
    # никогда бы не пересёкся — это гонка таймингов теста, не баг порога
    # (отдельно, без гонки, проверено в сценарии 5 ниже). Понижаем порог,
    # а не размер тела: так сквозной путь (анонс -> файл -> приём -> ACK)
    # проверяется по-настоящему, а не подменяется прямым вызовом.
    old_threshold = gate.BULK_THRESHOLD
    gate.BULK_THRESHOLD = 1000
    try:
        print("=== 2. выше порога -- уходит файлом, доходит байт-в-байт ===")
        fake = FakeFileClip()
        saved = patch_fileclip(fake)
        try:
            big = os.urandom(50_000)
            origin = start_body_server(big)
            stop, proxy, dial, listen, threads = start_gate()
            try:
                got = fetch_through_gate(proxy, origin, len(big))
                check("тело совпало байт-в-байт", got == big, f"{len(got)}/{len(big)}")
                check("fileclip.send_files звался хотя бы раз", fake.send_calls > 0, str(fake.send_calls))
            finally:
                stop_gate(stop, dial, listen, threads)
        finally:
            unpatch_fileclip(saved)

        print("=== 3. отправка файла сорвалась -- откат на ячейки, данные не теряются ===")
        fake = FakeFileClip(fail_send=True)
        saved = patch_fileclip(fake)
        try:
            big = os.urandom(50_000)
            origin = start_body_server(big)
            stop, proxy, dial, listen, threads = start_gate()
            try:
                got = fetch_through_gate(proxy, origin, len(big))
                check("тело всё равно дошло целиком (через ячейки)", got == big, f"{len(got)}/{len(big)}")
                check("попытка отправить файл была", fake.send_calls > 0, str(fake.send_calls))
            finally:
                stop_gate(stop, dial, listen, threads)
        finally:
            unpatch_fileclip(saved)

        print("=== 4. файл пришёл испорченным -- откат на ячейки, не тихая порча ===")
        fake = FakeFileClip(corrupt=True)
        saved = patch_fileclip(fake)
        try:
            big = os.urandom(50_000)
            origin = start_body_server(big)
            stop, proxy, dial, listen, threads = start_gate()
            try:
                got = fetch_through_gate(proxy, origin, len(big))
                check("тело всё равно дошло целиком и верно (через откат)", got == big, f"{len(got)}/{len(big)}")
                check("приём файла пробовался", fake.recv_calls > 0, str(fake.recv_calls))
            finally:
                stop_gate(stop, dial, listen, threads)
        finally:
            unpatch_fileclip(saved)
    finally:
        gate.BULK_THRESHOLD = old_threshold

    print("=== 5. _bulk_round: сериализовано -- только один поток за раз ===")

    class FakeTransport:
        peer_id = "A"

        def __init__(self):
            self.sent = []

        def send_many(self, frames, limit=None):
            self.sent.append(list(frames))

        def receive(self, timeout):
            return None

    fake = FakeFileClip()
    saved = patch_fileclip(fake)
    try:
        transport = FakeTransport()
        seq = __import__("rdpconn.app", fromlist=["SeqCounter"]).SeqCounter()
        pipes = {
            1: gate.Pipe(1),
            2: gate.Pipe(2),
        }
        pipes[1].to_peer.extend(os.urandom(gate.BULK_THRESHOLD + 1000))
        pipes[2].to_peer.extend(os.urandom(gate.BULK_THRESHOLD + 1000))
        busy = gate._bulk_round(transport, pipes, seq, dial=True)
        check("первый вызов занялся файлом", busy)
        started = [p for p in pipes.values() if p.bulk_out is not None]
        check("ровно один поток ушёл в файловый режим", len(started) == 1, str(len(started)))
    finally:
        unpatch_fileclip(saved)

    print()
    if _failed:
        print(f"RESULT: FAIL {_failed}")
        return 1
    print("RESULT: ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
