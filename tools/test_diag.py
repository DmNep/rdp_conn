"""Стартовая проверка штатного канала (rdpconn.diag): интернет/\\tsclient —
только информирование, не переключение, и с ограниченной задержкой даже
если обе проверки зависнут.

Сеть и диск этой машины — не то, на чём можно строить детерминированный
тест (есть ли тут реально интернет — неизвестно и не должно быть важно),
поэтому has_internet()/has_rdp_disk() проверяются на их собственной логике
(таймаут, листинг существующей/несуществующей папки), а startup_hint()'s
состав строки и бюджет задержки — через подмену самих функций.
"""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdpconn import diag

fails = []


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


print("=== 1. has_internet(): свой слушающий сокет -- True, закрытый порт -- False ===")
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]
try:
    check("слушающий localhost:PORT виден как 'интернет'",
         diag.has_internet(probe=("127.0.0.1", port), timeout=1.0))
finally:
    srv.close()

closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
closed.bind(("127.0.0.1", 0))
closed_port = closed.getsockname()[1]
closed.close()  # порт свободен и никто на нём не слушает -- соединение откажут сразу
check("закрытый порт -- не 'интернет'",
     not diag.has_internet(probe=("127.0.0.1", closed_port), timeout=1.0))

print("=== 2. has_rdp_disk(): существующая папка -- True, несуществующий путь -- False ===")
with tempfile.TemporaryDirectory() as tmp:
    check("существующая папка видна", diag.has_rdp_disk(root=tmp, timeout=1.0))
check("несуществующий путь -- False",
     not diag.has_rdp_disk(root=r"Z:\__rdpconn_test_no_such_path__", timeout=0.5))

print("=== 3. startup_hint(): обе проверки молчат -> None ===")
old_internet, old_disk = diag.has_internet, diag.has_rdp_disk
diag.has_internet = lambda timeout=0: False
diag.has_rdp_disk = lambda root=r"\\tsclient", timeout=0: False
try:
    check("ничего не найдено -> None", diag.startup_hint(timeout=0.3) is None)

    print("=== 4. startup_hint(): состав строки, когда найдено и то и другое ===")
    diag.has_internet = lambda timeout=0: True
    diag.has_rdp_disk = lambda root=r"\\tsclient", timeout=0: True
    hint = diag.startup_hint(timeout=0.3)
    check("строка не пустая", bool(hint), hint)
    check("упомянут интернет", "интернет" in (hint or ""), hint)
    check("упомянут tsclient", "tsclient" in (hint or ""), hint)
    check("явно сказано, что канал не переключается",
         "решение владельца" in (hint or "") or "не автоматическое" in (hint or ""), hint)

    print("=== 5. startup_hint(): обе проверки зависли -> бюджет задержки общий, не суммируется ===")

    def hang(*_a, **_kw):
        time.sleep(3.0)
        return True

    diag.has_internet = hang
    diag.has_rdp_disk = hang
    started = time.monotonic()
    result = diag.startup_hint(timeout=0.5)
    elapsed = time.monotonic() - started
    check("завис -> None, а не False-положительный результат", result is None, result)
    check("не ждали сумму таймаутов (двух по 3 с), а один общий бюджет",
         elapsed < 2.0, f"{elapsed:.2f} с")
finally:
    diag.has_internet, diag.has_rdp_disk = old_internet, old_disk

print()
if fails:
    print(f"RESULT: FAIL {len(fails)}")
    sys.exit(1)
print("RESULT: ALL GREEN")
