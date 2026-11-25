"""Слот с искусственной задержкой (DelayedClipboard, --slot sim:...): проверка
без второй машины и без живого RDP, что протокол не ломается на лаге редиректа.

Часть 1 — сам класс в памяти: запись не видна раньше срока, обгоняющая запись
побеждает, seq растёт только после применения. Часть 2 — tx и rx двумя
процессами через sim:DELAY:PATH, тем самым "хост и гуест прямо тут", о которых
шла речь: полный цикл CMD -> ACK -> NOTE должен дойти, просто не мгновенно.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rdpconn import clipboard as cb

PY = sys.executable
ENV = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")

_failed = 0


def check(name, ok, detail="") -> None:
    global _failed
    if ok:
        print(f"  {name}: OK  {detail}".rstrip())
    else:
        _failed += 1
        print(f"  {name}: FAIL  {detail}".rstrip())


print("=== 1. запись не видна раньше срока ===")
store = cb.DelayedClipboard(cb.MemoryClipboard(), delay=0.2, jitter=0.1)
store.write("первая")
check("сразу после write() ещё пусто", store.read() is None)
time.sleep(0.35)
check("после срока видно", store.read() == "первая")

print("=== 2. обгон: кто написал последним, тот и победил ===")
store = cb.DelayedClipboard(cb.MemoryClipboard(), delay=0.3, jitter=0.0)
store.write("устареет")
time.sleep(0.1)
store.write("актуальна")  # до срока первой — первая вообще не применится
time.sleep(0.35)
check("осталась только последняя запись", store.read() == "актуальна")

print("=== 3. seq растёт только когда запись применилась ===")
mem = cb.MemoryClipboard()
store = cb.DelayedClipboard(mem, delay=0.2)
before = store.seq()
store.write("x")
check("seq не вырос до срока", store.seq() == before)
time.sleep(0.25)
check("seq вырос после срока", store.seq() > before)

print("=== 4. open_store('sim:...') собирает нужную пару ===")
simple = cb.open_store("sim:0.1")
check("sim: без PATH — поверх memory",
      isinstance(simple, cb.DelayedClipboard) and isinstance(simple._store, cb.MemoryClipboard))

tmp = tempfile.mkdtemp(prefix="rdpconn_sim_")
path = os.path.join(tmp, "slot.txt")
withfile = cb.open_store(f"sim:0.15,0.05:{path}")
check("sim: с PATH — поверх file",
      isinstance(withfile, cb.DelayedClipboard) and isinstance(withfile._store, cb.FileClipboard))
withfile.write("привет")
check("файл ещё не тронут", not os.path.exists(path))
time.sleep(0.3)
withfile.read()  # settle() ленивый: применяется по обращению
check("файл появился после срока", os.path.exists(path))

try:
    cb.open_store("sim:не-число")
    check("плохая задержка — ошибка", False, "исключения не было")
except cb.ClipboardError:
    check("плохая задержка — ошибка", True)


def run(args, timeout):
    proc = subprocess.run(
        [PY, "-m", args[0]] + args[1:], cwd=ROOT, env=ENV,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    return proc.stdout, proc.stderr, proc.returncode


def start(args):
    return subprocess.Popen(
        [PY, "-m", args[0]] + args[1:], cwd=ROOT, env=ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )


print("=== 5. tx и rx двумя процессами через sim:DELAY:PATH ===")
slot = os.path.join(tempfile.mkdtemp(prefix="rdpconn_sim_duplex_"), "slot.txt")
rx = start(["rdpconn.rx", "--slot", f"sim:0.3,0.1:{slot}",
            "--peer-id", "B", "--exec", "--ping", "0", "--duration", "15"])
time.sleep(0.5)
tx_out, tx_err, tx_code = run(
    ["rdpconn.tx", "--slot", f"sim:0.3,0.1:{slot}",
     "--peer-id", "A", "--ping", "0", "--reply-wait", "4", "echo privet_iz_sim"],
    timeout=20,
)
check("tx завершился без ошибки", tx_code == 0, f"code={tx_code} err={tx_err!r}")
check("ответ дошёл несмотря на задержку", "privet_iz_sim" in tx_out, repr(tx_out))
rx.terminate()
rx_out, rx_err = rx.communicate(timeout=10)
check("rx принял команду", "<< CMD" in rx_out, repr(rx_out))

print()
print("RESULT: ALL GREEN" if not _failed else f"RESULT: FAIL ({_failed})")
sys.exit(1 if _failed else 0)
