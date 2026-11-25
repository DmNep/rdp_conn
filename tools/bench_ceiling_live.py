"""Прогон полного снимка через живой редирект.

host поднимает эхо на 127.0.0.1. guest идёт в прокси шлюза CONNECT-ом
на этот адрес и шлёт известное число байт. Время и потолок пишутся в отчёт.
Шлюз на обеих сторонах уже должен быть запущен.
"""

from __future__ import annotations

import argparse
import hashlib
import socket
import sys
import time
from pathlib import Path


def body_for(count: int) -> bytes:
    """Несжимаемый поток: иначе редирект сожмёт повтор одного байта и цифра врёт."""
    raw = bytearray()
    index = 0
    while len(raw) < count:
        raw += hashlib.sha256(index.to_bytes(4, "little")).digest()
        index += 1
    return bytes(raw[:count])


def serve(port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    srv.settimeout(180)
    print(f"эхо 127.0.0.1:{port}", flush=True)
    try:
        conn, _addr = srv.accept()
    except socket.timeout:
        print("никто не пришёл", flush=True)
        return
    with conn:
        conn.settimeout(180)
        got = _read_exact(conn, 4)
        if got is None:
            print("нет длины", flush=True)
            return
        count = int.from_bytes(got, "big")
        body = _read_exact(conn, count)
        if body is None:
            print(f"обрыв на {count}", flush=True)
            return
        conn.sendall(count.to_bytes(4, "big") + body)
        print(f"эхо вернул {count} байт", flush=True)


def client(proxy: str, port: int, count: int, report: Path) -> int:
    host, _, port_s = proxy.rpartition(":")
    sock = socket.create_connection((host or "127.0.0.1", int(port_s)), 15)
    sock.settimeout(180)
    started = time.monotonic()
    request = (
        f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n\r\n"
    ).encode("ascii")
    sock.sendall(request)
    head = b""
    while b"\r\n\r\n" not in head and len(head) < 4096:
        chunk = sock.recv(256)
        if not chunk:
            break
        head += chunk
    opened = time.monotonic()
    if b"200" not in head.split(b"\r\n", 1)[0]:
        text = f"прокси не открыл туннель: {head[:200]!r}\n"
        report.write_text(text, encoding="utf-8")
        print(text, flush=True)
        return 1
    body = body_for(count)
    sock.sendall(count.to_bytes(4, "big") + body)
    sent = time.monotonic()
    back = _read_exact(sock, 4 + count)
    done = time.monotonic()
    ok = back is not None and back[4:] == body
    upload = sent - opened
    total = done - started
    rate = count / upload if upload > 0 else 0
    text = (
        f"байт {count}\n"
        f"туннель {opened - started:.2f} с\n"
        f"туда {upload:.2f} с  {rate:.0f} Б/с\n"
        f"туда и обратно {total:.2f} с\n"
        f"эхо {'ДА' if ok else 'НЕТ'}\n"
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(text, encoding="utf-8")
    print(text, flush=True)
    return 0 if ok else 1


def _read_exact(sock: socket.socket, count: int) -> bytes | None:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(min(65536, count - len(buf)))
        if not chunk:
            return None
        buf += chunk
    return buf


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("host", "guest"))
    parser.add_argument("--port", type=int, default=18079)
    parser.add_argument("--proxy", default="127.0.0.1:8079")
    parser.add_argument("--bytes", type=int, default=262144)
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    if args.mode == "host":
        serve(args.port)
        return 0
    if not args.report:
        print("guest просит --report", flush=True)
        return 2
    return client(args.proxy, args.port, args.bytes, Path(args.report))


if __name__ == "__main__":
    sys.exit(main())
