"""gate (listen/dial) через sim: с инъецированной задержкой — без второй
машины и без живого RDP проверить, что прокси не упирается в скрытый
короткий таймер под лагом, которого раньше не было видно на memory-слоте
(tools/test_gate.py): там обмен мгновенный и ни одна пауза не проявляется.

dial_loop/listen_loop не похожи на старый баг tx (AGENTS.md 6): это
while not stop.is_set() без сдачи по таймауту, пауза внутри — только опрос.
Здесь это подтверждается эмпирически, не только чтением кода: GET через
туннель должен дойти, просто не мгновенно, и обогнать внутренние паузы
(_COALESCE_S=0.15, pause=0.15/0.3) явно, иначе тест ничего не доказывает.
"""
from __future__ import annotations

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


def start_server():
    port = free_port()
    ready = threading.Event()

    def run() -> None:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.settimeout(30)
        ready.set()
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            srv.close()
            return
        conn.settimeout(20)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\nhello_sim_1"
            )
        except OSError:
            pass
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(2)
    return port


def start_gate_delayed(delay: float, jitter: float, wait: float = 2.0):
    """Тот же стенд, что в test_gate.py, но каждая сторона пишет с задержкой."""
    cb.MemoryClipboard.reset()
    dial_store = cb.DelayedClipboard(cb.MemoryClipboard(), delay, jitter)
    listen_store = cb.DelayedClipboard(cb.MemoryClipboard(), delay, jitter)
    dial = cb.ClipboardTransport(dial_store, peer_id="A")
    listen = cb.ClipboardTransport(listen_store, peer_id="B")
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
        thread.join(5)
    dial.close()
    listen.close()


print("=== gate под sim: 2,0.5 — GET через туннель доходит, не мгновенно ===")
server_port = start_server()
stop, proxy_port, dial, listen, threads = start_gate_delayed(2.0, 0.5)

sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=20)
sock.settimeout(20)
t0 = time.monotonic()
sock.sendall(
    f"GET http://127.0.0.1:{server_port}/ HTTP/1.1\r\n"
    f"Host: 127.0.0.1:{server_port}\r\nConnection: close\r\n\r\n".encode()
)
data = b""
try:
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
except socket.timeout:
    pass
elapsed = time.monotonic() - t0
sock.close()

check("ответ сервера дошёл через прокси", b"hello_sim_1" in data, repr(data[:200]))
check("заняло дольше внутренних пауз шлюза (иначе задержка не сработала)",
      elapsed > 1.0, f"elapsed={elapsed:.2f}s")

stop_gate(stop, dial, listen, threads)

print()
print("RESULT: ALL GREEN" if not _failed else f"RESULT: FAIL ({_failed})")
sys.exit(1 if _failed else 0)
