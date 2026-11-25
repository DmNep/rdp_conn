"""Служебная ячейка S1: одна на снимок, данные не вытесняет, кодер её не видит.

Секрет из кадра данных не попадает ни в ячейку, ни в сводку хоста.
"""
import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rdpconn import clipboard as cb
from rdpconn import config, framing, statcell

fails = []
SECRET = b"SECRET-PROMPT"


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def pair():
    cb.MemoryClipboard.reset()
    guest = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
    host = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
    statcell.attach_guest(guest, statcell.FEED_MODEL)
    host.picture = statcell.Picture("model")
    return guest, host


def note(text: bytes) -> bytes:
    return framing.build_frame(config.NOTE, 1, text)


def split(transport):
    stats, data = [], []
    for _who, chain in transport._parse(transport._store.read() or "").items():
        for _num, b64 in chain:
            raw = base64.b64decode(b64)
            (stats if statcell.is_frame(raw) else data).append(raw)
    return stats, data


def take(host, timeout=0.2):
    got = []
    while True:
        raw = host.receive(timeout)
        if raw is None:
            return got
        got.append(raw)
        timeout = 0.05


board = statcell.Board(statcell.FEED_MODEL)
board.note(2, 10, 1)
first = statcell.unpack(board.frame())
check("первая ячейка без прошлого снимка", first == {
    "feed": statcell.FEED_MODEL, "err": 0, "procs": 1, "queued": 10, "prev": 0,
}, str(first))
board.fail(statcell.ERR_CODER)
board.note(1, 4, 1)
second = statcell.unpack(board.frame())
check("ошибка уходит следующим снимком", second["err"] == statcell.ERR_CODER and second["prev"] == 2,
      str(second))
packed = statcell.pack(statcell.FEED_NET, 3, 100, 40, statcell.ERR_SEND)
parsed = framing.parse_frame(packed)
check("ячейка короче потолка кадра", len(parsed.payload) == statcell._BODY and len(parsed.payload) <= config.MAX_PAYLOAD)
try:
    framing.build_frame(config.NOTE, 0, b"x" * (config.MAX_PAYLOAD + 1))
    check("кадр длиннее 240 по-прежнему ошибка", False)
except ValueError:
    check("кадр длиннее 240 по-прежнему ошибка", True)
check("чужая магия не ячейка", not statcell.is_frame(note(b"C1not-stats")))

guest, host = pair()
frames = [note(b"one"), note(SECRET), note(b"three")]
guest.board.note(len(frames), 50, 1)
guest.send_many(frames, limit=3)
stats, data = split(guest)
check("три кадра данных и одна ячейка", len(data) == 3 and len(stats) == 1, f"data={len(data)} stats={len(stats)}")
check("секрет не в ячейке", SECRET not in stats[0])
info = statcell.unpack(stats[0])
check("в ячейке очередь и режим", info["queued"] == 50 and info["procs"] == 1 and info["prev"] == 0, str(info))

replay = [base64.b64decode(b64) for _num, b64 in reversed(guest._my_history)]
guest._publish_many(replay, keep_history=False, schedule_nudge=False)
stats, data = split(guest)
check("повтор не двоит ячейку", len(stats) == 1 and len(data) == 3, f"data={len(data)} stats={len(stats)}")

got = take(host)
check("хост отдал только данные", len(got) == 3 and all(not statcell.is_frame(raw) for raw in got), str(len(got)))
check("секрет дошёл в данных", any(SECRET in raw for raw in got))
snap = host.picture.snapshot()
blob = json.dumps(snap, ensure_ascii=False)
check("сводка без секрета", SECRET.decode() not in blob, blob)
check("сводка гуеста", snap["guest_procs"] == 1 and snap["guest_queued"] == 50 and snap["guest_prev"] == 0
      and snap["guest_feed"] == "модель", blob)
check("байты из рдп без ячейки", snap["from"] == sum(len(raw) for raw in got), str(snap["from"]))

guest.board.note(1, 0, 1)
guest.send_many([note(b"next")], limit=3)
got = take(host)
check("второй снимок помнит прошлое число ячеек",
      len(got) == 1 and host.picture.snapshot()["guest_prev"] == 3,
      str(host.picture.guest))

cb.MemoryClipboard.reset()
plain = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
other = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
plain.send_many([note(b"solo")], limit=3)
stats, data = split(plain)
check("без доски ячейки нет", len(stats) == 0 and len(data) == 1, f"data={len(data)} stats={len(stats)}")
check("собеседник получил кадр", other.receive(0.2) == note(b"solo"))

print()
if fails:
    print(f"FAILED {len(fails)}: {', '.join(fails)}")
    sys.exit(1)
print("ALL GREEN")
