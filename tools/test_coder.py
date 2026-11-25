"""Текст кодера через буфер: слот в памяти, два окна, ошибка модели."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdpconn import clipboard as cb
from rdpconn.coder import CELLS, CHUNK, CoderError, Link, ask, encode, serve
from rdpconn.framing import build_frame, parse_frame
from rdpconn import config

fails = []


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def pair(cells: int):
    cb.MemoryClipboard.reset()
    host = Link(cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A", auto_pause=False),
                cells=cells, reply_wait=3)
    guest = Link(cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B", auto_pause=False),
                 cells=cells, reply_wait=3)
    return host, guest


print("=== 1. кусок не длиннее кадра ===")
raw = encode(1, 0, 1, 0, b"x" * CHUNK)
frame = build_frame(config.NOTE, 1, raw)
parsed = parse_frame(frame)
check("кадр разбирается", parsed.payload == raw)
try:
    encode(1, 0, 1, 0, b"x" * (CHUNK + 1))
    check("длинный кусок отвергнут", False)
except ValueError:
    check("длинный кусок отвергнут", True)

print("=== 2. запрос в два окна и ответ модели ===")
host, guest = pair(1)
seen = {}


def complete(messages):
    seen["n"] = len(messages[-1]["content"])
    yield "text", "pon"
    yield "text", "g"


def run_host():
    try:
        serve(host, complete, once=True)
    except Exception as exc:
        seen["err"] = str(exc)


thread = threading.Thread(target=run_host)
thread.start()
body = os.urandom(500)
got = []
try:
    for kind, text in ask(guest, {"messages": [{"role": "user", "content": body.hex()}]}):
        got.append((kind, text))
except CoderError as exc:
    seen["ask"] = str(exc)
thread.join(5)
check("хозяин жив", "err" not in seen, seen.get("err", ""))
check("запрос доехал целиком", seen.get("n") == len(body.hex()), f"n={seen.get('n')}")
check("ответ pong", "".join(text for _, text in got) == "pong", repr(got))
check("поток дождался конца", not thread.is_alive())

print("=== 3. ошибка модели доходит текстом ===")
host, guest = pair(CELLS)


def broken(_messages):
    raise CoderError("нет модели")
    yield "text", ""


thread = threading.Thread(target=lambda: serve(host, broken, once=True))
thread.start()
try:
    list(ask(guest, {"system": "ты кодер", "messages": [{"role": "user", "content": "ping"}]}))
    check("ошибка пришла", False)
except CoderError as exc:
    check("ошибка пришла", "нет модели" in str(exc), str(exc))
thread.join(5)
check("поток ошибки кончился", not thread.is_alive())

print()
if fails:
    print(f"RESULT: FAIL {len(fails)}")
    sys.exit(1)
print("RESULT: ALL GREEN")
