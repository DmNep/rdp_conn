"""Хронический простой gate: предупреждение про возможную деградацию
RDP-сессии, не просто "пауза редиректа" (app.CHRONIC_STALL_S, см.
rdpconn/app.py). Найдено 27.11.2026 на живом стенде: dial_loop молчал в
логе полностью, пока направление B->A не отвечало вовсе несколько минут
подряд — человек не видел вообще никакого сигнала. Порог занижен здесь
монки-патчем app.CHRONIC_STALL_S (доли секунды вместо 3 минут).
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rdpconn import app
from rdpconn import clipboard as cb
from rdpconn import gate

fails = []


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def capture_warnings():
    captured: list[str] = []
    original = app.warn

    def fake(text: str) -> None:
        captured.append(text)

    app.warn = fake
    return captured, original


def has_hint(messages: list[str]) -> bool:
    return any("деградаци" in m or "переподключ" in m for m in messages)


print("=== 1. dial_loop: ни одного кадра от собеседника вовсе ===")
old_threshold = app.CHRONIC_STALL_S
app.CHRONIC_STALL_S = 0.3
captured, original_warn = capture_warnings()
try:
    cb.MemoryClipboard.reset()
    dial = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
    stop = threading.Event()
    thread = threading.Thread(target=gate.dial_loop, args=(dial, stop, 0.1), daemon=True)
    thread.start()
    time.sleep(1.5)
    stop.set()
    thread.join(3)
    dial.close()
finally:
    app.warn = original_warn
    app.CHRONIC_STALL_S = old_threshold
check("предупреждение про деградацию сессии появилось", has_hint(captured), captured[:3])

print("=== 2. listen_loop: клиент подключился, ответа от dial нет вовсе ===")
old_threshold = app.CHRONIC_STALL_S
app.CHRONIC_STALL_S = 0.3
captured, original_warn = capture_warnings()
client = None
try:
    cb.MemoryClipboard.reset()
    listen = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
    stop = threading.Event()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    proxy_port = probe.getsockname()[1]
    probe.close()
    thread = threading.Thread(
        target=gate.listen_loop, args=(listen, "127.0.0.1", proxy_port, stop, 0.1), daemon=True)
    thread.start()
    time.sleep(0.2)
    client = socket.create_connection(("127.0.0.1", proxy_port), timeout=2)
    client.sendall(
        b"GET http://127.0.0.1:1/ HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
    time.sleep(1.5)
    stop.set()
    thread.join(3)
    listen.close()
finally:
    if client is not None:
        client.close()
    app.warn = original_warn
    app.CHRONIC_STALL_S = old_threshold
check("предупреждение про деградацию сессии появилось", has_hint(captured), captured[:5])

print("=== 3. один раз на серию -- не спамит на каждом цикле бэкоффа ===")
old_threshold = app.CHRONIC_STALL_S
app.CHRONIC_STALL_S = 0.1
captured, original_warn = capture_warnings()
try:
    cb.MemoryClipboard.reset()
    dial = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
    stop = threading.Event()
    thread = threading.Thread(target=gate.dial_loop, args=(dial, stop, 0.05), daemon=True)
    thread.start()
    time.sleep(2.0)
    stop.set()
    thread.join(3)
    dial.close()
finally:
    app.warn = original_warn
    app.CHRONIC_STALL_S = old_threshold
hits = sum(1 for m in captured if "деградаци" in m or "переподключ" in m)
check("предупреждение встретилось хотя бы раз", hits >= 1, f"hits={hits}")
check("но не на каждом тике цикла (не десятки повторов)", hits <= 2, f"hits={hits}")

print()
if fails:
    print(f"RESULT: FAIL {len(fails)}")
    sys.exit(1)
print("RESULT: ALL GREEN")
