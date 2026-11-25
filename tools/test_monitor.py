"""Наблюдение за слотом ничего не пишет и не показывает текст копий."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdpconn import clipboard as cb
from rdpconn import config
from rdpconn.coder import FLAG_WINDOW, OP_PIECE, encode
from rdpconn.framing import build_frame
from rdpconn.monitor import Watch

fails = []


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def cell(who, num, raw):
    b64 = base64.b64encode(raw).decode("ascii")
    return f"RDP1|{who}:{num}:{b64}"


secret = "SECRET-PROMPT"
frame = build_frame(config.NOTE, 1, encode(OP_PIECE, FLAG_WINDOW, 1, 0, secret.encode()))
slot = cell("B", 7, frame)

print("=== 1. первое чтение не считается трафиком ===")
watch = Watch()
watch.note(slot, seed=True)
shot = watch.snapshot()
check("сессия пустая", shot["up"] == 0 and shot["frames"] == 0)
check("слот обмен", shot["slot"] == "обмен")

print("=== 2. новая ячейка видна, текст запроса нет ===")
watch.note(slot + f"|B:8:{base64.b64encode(frame).decode('ascii')}")
shot = watch.snapshot()
check("байт прибавилось", shot["up"] == len(frame), str(shot["up"]))
check("сторона B", shot["peers"] == [{"id": "B", "frames": 1, "bytes": len(frame)}])
blob = json.dumps(shot, ensure_ascii=False)
check("текст запроса не на странице", secret not in blob)
check("подпись кодера", any("кодер запрос" in line for line in shot["events"]))

print("=== 3. та же ячейка второй раз не удваивается ===")
watch.note(slot + f"|B:8:{base64.b64encode(frame).decode('ascii')}")
check("байт те же", watch.snapshot()["up"] == len(frame))

print("=== 4. чужая копия без текста ===")
watch.note("не показывай " + secret)
shot = watch.snapshot()
check("слот чужой", shot["slot"] == "чужая копия" and shot["hold"] == "чужая копия")
check("текст копии не на странице", secret not in json.dumps(shot, ensure_ascii=False))

print("=== 5. в рдп и из рдп, уплотнение, два пакета, пик ===")
host = Watch(local="A")
one = cell("A", 1, frame)
host.note(one)
check("в рдп", host.snapshot()["into"] == len(frame) and host.snapshot()["from"] == 0)
other = build_frame(config.NOTE, 2, encode(OP_PIECE, FLAG_WINDOW, 2, 0, b"y"))
packed = cell("B", 1, frame) + "|" + f"B:2:{base64.b64encode(other).decode('ascii')}"
guest = Watch(local="A")
guest.note(packed)
shot = guest.snapshot()
check("из рдп", shot["from"] == len(frame) + len(other) and shot["into"] == 0)
check("уплотнение", "уплотнение, 2 ячеек" in shot["pack"], shot["pack"])
again = cell("A", 9, frame)
host.note(again)
check("два пакета", "два пакета" in host.snapshot()["pack"], host.snapshot()["pack"])
host._into_max = 50
host.reset_peaks()
check("пик сброшен", host._into_max == 0 and host._out_max == 0)
check("сессия на месте", host.into == len(frame) * 2)

print("=== 6. чтение памяти не пишет ===")
cb.MemoryClipboard.reset()
store = cb.MemoryClipboard()
before = store.seq()
check("try_read пусто", store.try_read() is None)
check("номер не сдвинулся", store.seq() == before)

print()
if fails:
    print(f"FAIL {len(fails)}: {', '.join(fails)}")
    sys.exit(1)
print("ALL GREEN")
