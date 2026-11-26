"""Встраивание F1 в ends.py: on_tick (coder.py) и DropWatcher (fileshake.py).

Слот в памяти, fileclip.* подменены — реальный OLE/shell32 нужен только на
живом стенде (см. tools/test_fileshake.py и память проекта). Здесь проверяется
протокольная часть: что чужой кадр на простое не теряется и не мешает
следующему настоящему запросу, и что DropWatcher правильно гонит папку.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdpconn import clipboard as cb
from rdpconn import fileclip, fileshake
from rdpconn.coder import CoderError, Link, ask, serve
from rdpconn.fileshake import DropWatcher, Handshake, HandshakeError

fails = []


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def coder_pair(cells: int = 4):
    cb.MemoryClipboard.reset()
    host = Link(cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A", auto_pause=False),
                cells=cells, reply_wait=3)
    guest = Link(cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B", auto_pause=False),
                 cells=cells, reply_wait=3)
    return host, guest


print("=== 1. on_tick видит чужой кадр в простое, не мешает следующему запросу ===")
host, guest = coder_pair()
seen = {"ticks": []}


def complete(messages):
    yield "text", "pong"


def run_host():
    try:
        serve(host, complete, once=True)
    except Exception as exc:
        seen["err"] = str(exc)


# Чужой кадр (не C1) в слоте до того, как гость вообще отправит запрос —
# ровно то, что увидит _read_window() на первом же проходе с chunks==[].
# Публикует guest.transport, не host.transport: receive() никогда не отдаёт
# транспорту его же собственные записи (_unseen_foreign пропускает свой
# peer_id), поэтому с чужим кадром сторону подделывает именно собеседник.
foreign = fileshake.Handshake(guest.transport)._frame(fileshake.OP_OFFER, b'{"f":[],"c":1,"e":0.0}')
guest.transport.send(foreign)
host.on_tick = lambda raw: seen["ticks"].append(raw is not None)

thread = threading.Thread(target=run_host)
thread.start()
got = []
try:
    for kind, text in ask(guest, {"messages": [{"role": "user", "content": "hi"}]}):
        got.append((kind, text))
except CoderError as exc:
    seen["ask"] = str(exc)
thread.join(5)
check("хозяин жив", "err" not in seen, seen.get("err", ""))
check("on_tick дёрнулся хотя бы раз с кадром", any(seen["ticks"]), seen["ticks"])
check("запрос после чужого кадра всё равно дошёл", "".join(t for _, t in got) == "pong", got)
check("поток кончился", not thread.is_alive())

print("=== 2. DropWatcher: входящий FILE_OFFER принят, FILE_DONE уходит ===")
cb.MemoryClipboard.reset()
a = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A", auto_pause=False)
b = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B", auto_pause=False)
inbox_dir = tempfile.mkdtemp(prefix="fileshake_drop_in_")


def fake_wait_for_new_file(since_seq, timeout):
    return since_seq + 1


def fake_list_clipboard_files(**kwargs):
    return [fileclip.FileEntry(name="hello.txt", size=5)]


def fake_recv_file(index, out_path, chunk=524288):
    Path(out_path).write_bytes(b"hello")
    return fileclip.RecvResult(bytes_total=5, first_byte_s=0.0, total_s=0.0)


real_wait_for_new_file = fileclip.wait_for_new_file
real_list_clipboard_files = fileclip.list_clipboard_files
real_recv_file = fileclip.recv_file
fileclip.wait_for_new_file = fake_wait_for_new_file
fileclip.list_clipboard_files = fake_list_clipboard_files
fileclip.recv_file = fake_recv_file
try:
    receiver = DropWatcher(b, Path(inbox_dir) / "drop")
    hs = Handshake(a)
    # Кадр оффера собираем напрямую: offer() сам читает размер файла с диска,
    # а здесь важно только содержимое JSON, не настоящий файл на стороне A.
    raw_offer = hs._frame(fileshake.OP_OFFER,
                          __import__("json").dumps({"f": [["hello.txt", 5]], "c": 524288, "e": 0.0}).encode())
    a.send(raw_offer)
    raw = b.receive(2.0)
    check("приёмник увидел FILE_OFFER", raw is not None)
    receiver.tick(raw)
    ready = a.receive(2.0)
    ready_parsed = fileshake._note(ready) if ready is not None else None
    check("FILE_READY дошёл отправителю", ready_parsed is not None and ready_parsed[0] == fileshake.OP_READY,
         ready_parsed)
    done = a.receive(2.0)
    check("FILE_DONE дошёл отправителю", done is not None)
    if done is not None:
        parsed = fileshake._note(done)
        check("это FILE_DONE, не FILE_FAIL", parsed is not None and parsed[0] == fileshake.OP_DONE, parsed)
    got_file = Path(inbox_dir) / "drop" / "in" / "hello.txt"
    check("файл лёг в in/", got_file.exists() and got_file.read_bytes() == b"hello")
finally:
    fileclip.wait_for_new_file = real_wait_for_new_file
    fileclip.list_clipboard_files = real_list_clipboard_files
    fileclip.recv_file = real_recv_file

print("=== 3. DropWatcher: файл в out/ уходит, после ACK перемещается в sent/ ===")
cb.MemoryClipboard.reset()
a = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A", auto_pause=False)
b = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B", auto_pause=False)
drop_root = tempfile.mkdtemp(prefix="fileshake_drop_out_")
drop_dir = Path(drop_root) / "drop"
(drop_dir / "out").mkdir(parents=True)
(drop_dir / "out" / "a.bin").write_bytes(os.urandom(10))

real_send_files = fileclip.send_files
fileclip.send_files = lambda paths: None
try:
    sender = DropWatcher(a, drop_dir, chunk=1024)
    result = {}

    def peer():
        hs = Handshake(b)
        offer = hs.wait_offer(5.0)
        result["offer"] = offer
        if offer is not None:
            hs.accept()
            hs.report_done([(name, size, "deadbeef") for name, size in offer.files])

    thread = threading.Thread(target=peer)
    thread.start()
    sender.tick(None)  # находит a.bin, шлёт offer, дожидается ready+done
    thread.join(5)
    check("собеседник увидел offer", result.get("offer") is not None)
    check("файл переехал в sent/", (drop_dir / "out" / "sent" / "a.bin").exists())
    check("из out/ файл убран", not (drop_dir / "out" / "a.bin").exists())
finally:
    fileclip.send_files = real_send_files

print("=== 4. DropWatcher: неудача — отступ, повтор не долбит сразу ===")
cb.MemoryClipboard.reset()
a = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A", auto_pause=False)
drop_root2 = tempfile.mkdtemp(prefix="fileshake_drop_fail_")
drop_dir2 = Path(drop_root2) / "drop"
(drop_dir2 / "out").mkdir(parents=True)
(drop_dir2 / "out" / "b.bin").write_bytes(b"x")
# Никто не ответит FILE_READY — offer() упрётся в свой же ready_timeout.
sender2 = DropWatcher(a, drop_dir2, outbox_retry_s=5.0)
orig_send = fileshake.send_files


def failing_send(*a_, **kw):
    raise HandshakeError("FILE_READY не дождались")


fileshake.send_files = failing_send
try:
    t0 = time.monotonic()
    sender2.tick(None)
    check("файл остался в out/ (не ушёл в sent/)", (drop_dir2 / "out" / "b.bin").exists())
    check("второй tick в пределах отступа не трогает outbox",
          sender2._outbox_fail_until > t0)
finally:
    fileshake.send_files = orig_send

print()
if fails:
    print(f"RESULT: FAIL {len(fails)}")
    sys.exit(1)
print("RESULT: ALL GREEN")
