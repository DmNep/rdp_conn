"""После объявления обрыва PING не прекращаются, а идут реже (Heartbeat,
app.py): раньше due() выключал проактивные PING насовсем, как только
mark_lost() срабатывал, и без PING от человека канал не заметил бы возврата
собеседника сам — только следующей набранной командой. Для coder/gate/ends,
где человека за пультом нет, это значило зависание до ручного перезапуска.

Проверяется сам класс Heartbeat напрямую, без процессов: так дешевле и не
зависит от таймингов ОС.
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rdpconn import app

_failed = 0


def check(name, ok, detail="") -> None:
    global _failed
    if ok:
        print(f"  {name}: OK  {detail}".rstrip())
    else:
        _failed += 1
        print(f"  {name}: FAIL  {detail}".rstrip())


print("=== 1. живой собеседник: due() идёт с обычным интервалом ===")
hb = app.Heartbeat(interval=1.0, misses=3)
hb._last_ping = time.monotonic() - 1.01
check("пора пинговать", hb.due())
hb.ping()
check("сразу после пинга — рано", not hb.due())

print("=== 2. после mark_lost PING не исчезают, а удваиваются ===")
hb = app.Heartbeat(interval=1.0, misses=3)
hb.mark_lost()
check("после mark_lost всё ещё due() (а не выключено навсегда)",
      not hb.due(), "рано: с момента mark_lost ничего не прошло")
hb._last_ping = time.monotonic() - 1.01
check("через interval — первая разведка", hb.due())
hb.ping()
gap1 = hb._dead_gap
check("пауза выросла до 2x interval", abs(gap1 - 2.0) < 1e-6, f"gap={gap1}")
hb._last_ping = time.monotonic() - (gap1 + 0.01)
check("следующая — через новую (бóльшую) паузу", hb.due())
hb.ping()
gap2 = hb._dead_gap
check("пауза снова выросла (4x interval)", abs(gap2 - 4.0) < 1e-6, f"gap={gap2}")

print("=== 3. потолок бэкоффа не превышается ===")
hb = app.Heartbeat(interval=200.0, misses=3)
hb.mark_lost()
hb._dead_gap = 250.0
hb._last_ping = time.monotonic() - 251.0
check("due() на подходе к потолку", hb.due())
hb.ping()
check("пауза срезана потолком RECONNECT_BACKOFF_MAX_S",
      hb._dead_gap == app.RECONNECT_BACKOFF_MAX_S, f"gap={hb._dead_gap}")

print("=== 4. возврат собеседника сбрасывает бэкофф ===")
hb = app.Heartbeat(interval=1.0, misses=3)
hb.mark_lost()
hb._last_ping = time.monotonic() - 1.01
hb.ping()
check("бэкофф успел вырасти", hb._dead_gap > 1.0, f"gap={hb._dead_gap}")
revived = hb.note()
check("note() сообщил о возврате", revived)
check("бэкофф сброшен", hb._dead_gap == 0.0, f"gap={hb._dead_gap}")
hb._last_ping = time.monotonic() - 1.01
check("due() снова по обычному interval, не по старому бэкоффу", hb.due())

print("=== 5. без proactive (rx) due() всегда False — бэкофф его не касается ===")
hb = app.Heartbeat(interval=1.0, misses=3, proactive=False)
hb.mark_lost()
hb._last_ping = time.monotonic() - 100.0
check("приёмник своих PING не шлёт и после обрыва", not hb.due())

print()
print("RESULT: ALL GREEN" if not _failed else f"RESULT: FAIL ({_failed})")
sys.exit(1 if _failed else 0)
