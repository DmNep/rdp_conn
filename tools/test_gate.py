"""Прокси через слот в памяти: обычный GET и CONNECT, как у Коды.

Сервер локальный. Интернет и Windows-буфер не нужны.
"""
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rdpconn import clipboard as cb
from rdpconn import gate

_failed = 0


def check(name, ok, detail="") -> None:
    global _failed
    if ok:
        print(f"  {name}: OK  {detail}".rstrip())
    else:
        _failed += 1
        print(f"  {name}: FAIL  {detail}".rstrip())


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def start_server() -> int:
    port = free_port()
    ready = threading.Event()

    def run() -> None:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.settimeout(8)
        ready.set()
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            srv.close()
            return
        conn.settimeout(5)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"
            )
        except OSError:
            pass
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(2)
    return port


def start_gate(wait: float = 2.0):
    cb.MemoryClipboard.reset()
    dial = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
    listen = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
    stop = threading.Event()
    proxy = free_port()
    threads = [
        threading.Thread(target=gate.dial_loop, args=(dial, stop, wait), daemon=True),
        threading.Thread(
            target=gate.listen_loop,
            args=(listen, "127.0.0.1", proxy, stop, wait),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.15)
    return stop, proxy, dial, listen, threads


def stop_gate(stop, dial, listen, threads) -> None:
    stop.set()
    for thread in threads:
        thread.join(3)
    dial.close()
    listen.close()


def read_until(sock: socket.socket, needle: bytes) -> bytes:
    sock.settimeout(8)
    buf = b""
    while needle not in buf:
        chunk = sock.recv(1024)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    return buf


def main() -> int:
    print("=== 1. GET cherez proksi ===")
    origin = start_server()
    stop, proxy, dial, listen, threads = start_gate()
    try:
        client = socket.create_connection(("127.0.0.1", proxy), timeout=5)
        client.sendall(
            f"GET http://127.0.0.1:{origin}/ HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Connection: close\r\n\r\n".encode()
        )
        body = read_until(client, b"hello")
        client.close()
        check("otvet hello", b"hello" in body, repr(body[:120]))
    finally:
        stop_gate(stop, dial, listen, threads)

    print("=== 2. CONNECT, kak u Kody na HTTPS ===")
    origin = start_server()
    stop, proxy, dial, listen, threads = start_gate()
    try:
        client = socket.create_connection(("127.0.0.1", proxy), timeout=5)
        client.sendall(
            f"CONNECT 127.0.0.1:{origin} HTTP/1.1\r\nHost: 127.0.0.1:{origin}\r\n\r\n".encode()
        )
        client.settimeout(8)
        opened = b""
        while b"\r\n\r\n" not in opened:
            chunk = client.recv(1024)
            if not chunk:
                break
            opened += chunk
        check("tunnel 200", b"200" in opened.split(b"\r\n", 1)[0], repr(opened[:80]))
        client.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        body = read_until(client, b"hello")
        client.close()
        check("skvoz' tunnel hello", b"hello" in body, repr(body[:120]))
    finally:
        stop_gate(stop, dial, listen, threads)

    print("=== 3. pauza klavishei derzhit slot s obeikh storon ===")
    origin = start_server()
    stop, proxy, dial, listen, threads = start_gate()
    try:
        client = socket.create_connection(("127.0.0.1", proxy), timeout=5)
        client.sendall(
            f"GET http://127.0.0.1:{origin}/ HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Connection: close\r\n\r\n".encode()
        )
        time.sleep(0.4)
        dial.pause()
        deadline = time.time() + 2
        while time.time() < deadline and not listen.paused:
            time.sleep(0.05)
        check("slushatel tozhe vstal", dial.paused and listen.paused)
        held = cb.MemoryClipboard().read()
        time.sleep(1.3)
        check("cherez sekundu slot tot zhe", cb.MemoryClipboard().read() == held)
        dial.resume()
        deadline = time.time() + 2
        while time.time() < deadline and listen.paused:
            time.sleep(0.05)
        check("povtornaya klavisha prodolzhaet", not dial.paused and not listen.paused)
        body = read_until(client, b"hello")
        client.close()
        check("otvet doshel posle prodolzheniya", b"hello" in body, repr(body[:80]))
    finally:
        stop_gate(stop, dial, listen, threads)

    print("=== 4. tekst cheloveka ne zatiraetsya, vstavka vozvrashchaet kanal ===")
    origin = start_server()
    stop, proxy, dial, listen, threads = start_gate(0.3)
    try:
        for side in (dial, listen):
            side.human_cap_s = 3.0
            side.human_quiet_s = 0.4
        client = socket.create_connection(("127.0.0.1", proxy), timeout=5)
        client.sendall(
            f"GET http://127.0.0.1:{origin}/ HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Connection: close\r\n\r\n".encode()
        )
        marker = "tekst cheloveka"
        cb.MemoryClipboard().write(marker)
        deadline = time.time() + 2
        while time.time() < deadline and not (dial.yielding and listen.yielding):
            time.sleep(0.05)
        check("obe storony zamolchali", dial.yielding and listen.yielding)
        time.sleep(0.25)
        dial.note_paste()
        listen.note_paste()
        time.sleep(0.25)
        check("posle vstavki eshche molchat", dial.yielding and listen.yielding)
        check("tekst na meste", cb.MemoryClipboard().read() == marker)
        deadline = time.time() + 2
        while time.time() < deadline and (dial.yielding or listen.yielding):
            time.sleep(0.05)
        check("po sroku obmen vernulsya", not dial.yielding and not listen.yielding)
        body = read_until(client, b"hello")
        client.close()
        check("otvet doshel", b"hello" in body, repr(body[:80]))
        check("slott uzhe ne tekst cheloveka", cb.MemoryClipboard().read() != marker)
    finally:
        stop_gate(stop, dial, listen, threads)

    print("RESULT:", "ALL GREEN" if _failed == 0 else f"{_failed} FAILED")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
