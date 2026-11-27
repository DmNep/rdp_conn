"""Мьютекс single-instance (rdpconn.singleton): захват по role+peer_id,
отказ при живом владельце, автоосвобождение при его смерти, --replace
вытесняет явно.

Второй процесс — настоящий subprocess, как в test_duplex.py: конфликт
локальный (одна машина, одна история ячеек под одним peer_id), второй
машины для его проверки не нужно.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rdpconn import singleton

fails = []


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def spawn_holder(role: str, peer_id: str) -> subprocess.Popen:
    """Отдельный процесс, который захватывает (role, peer_id) и держит его,
    пока не умрёт (сам или от kill()) — print("held") синхронизирует момент
    захвата с этим тестом, как уже делает test_duplex.py для tx/rx."""
    code = (
        f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
        "from rdpconn import singleton\n"
        f"lock = singleton.acquire({role!r}, {peer_id!r})\n"
        "print('held', flush=True)\n"
        "import time; time.sleep(30)\n"
    )
    return subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, text=True)


def stop_holder(p: subprocess.Popen) -> None:
    if p.poll() is None:
        p.kill()
        p.wait(5)


if sys.platform != "win32":
    print("не Windows: мьютекс не из чего собрать, acquire() сознательно заглушка")
    print()
    print("RESULT: ALL GREEN")
    sys.exit(0)

print("=== 1. имя мьютекса различает role и peer_id ===")
check("разные role -> разные имена",
      singleton._mutex_name("tx", "A") != singleton._mutex_name("rx", "A"))
check("разные peer_id -> разные имена",
      singleton._mutex_name("tx", "A") != singleton._mutex_name("tx", "B"))
check("без peer_id тоже детерминировано (не None каждый раз заново)",
      singleton._mutex_name("tx", None) == singleton._mutex_name("tx", None))

print("=== 2. свободный мьютекс захватывается и отпускается ===")
lock = singleton.acquire("singleton-test", "T1")
check("acquire() вернул Lock", lock is not None)
lock.close()
lock2 = singleton.acquire("singleton-test", "T1")
check("после close() тот же (role, peer_id) снова свободен", lock2 is not None)
lock2.close()

print("=== 3. конфликт с живым владельцем в отдельном процессе ===")
holder = spawn_holder("singleton-test", "T2")
try:
    line = holder.stdout.readline()
    check("держатель поднялся", line.strip() == "held", repr(line))
    time.sleep(0.2)
    try:
        singleton.acquire("singleton-test", "T2")
        check("второй acquire() без --replace отказан", False)
    except singleton.SingletonError as exc:
        check("второй acquire() без --replace отказан",
             str(holder.pid) in str(exc), str(exc))

    print("=== 4. --replace вытесняет живого владельца ===")
    lock3 = singleton.acquire("singleton-test", "T2", replace=True)
    check("acquire(replace=True) не бросил", lock3 is not None)
    time.sleep(0.2)
    check("прежний держатель в самом деле завершён", holder.poll() is not None)
    lock3.close()
finally:
    stop_holder(holder)

print("=== 5. мёртвый владелец не нужно вытеснять явно (ОС освободила сама) ===")
holder2 = spawn_holder("singleton-test", "T3")
line = holder2.stdout.readline()
check("держатель #2 поднялся", line.strip() == "held", repr(line))
holder2.kill()
holder2.wait(5)
time.sleep(0.3)
try:
    lock4 = singleton.acquire("singleton-test", "T3")
    check("acquire() без --replace проходит после смерти владельца", True)
    lock4.close()
except singleton.SingletonError as exc:
    check("acquire() без --replace проходит после смерти владельца", False, str(exc))

print("=== 6. разные peer_id той же роли не конфликтуют ===")
lock_a = singleton.acquire("singleton-test", "T4a")
try:
    lock_b = singleton.acquire("singleton-test", "T4b")
    check("T4a и T4b захватываются одновременно", True)
    lock_b.close()
except singleton.SingletonError as exc:
    check("T4a и T4b захватываются одновременно", False, str(exc))
finally:
    lock_a.close()

print("=== 7. разные роли того же peer_id не конфликтуют ===")
lock_tx = singleton.acquire("singleton-test-tx", "T5")
try:
    lock_rx = singleton.acquire("singleton-test-rx", "T5")
    check("две роли на одном peer_id захватываются одновременно", True)
    lock_rx.close()
except singleton.SingletonError as exc:
    check("две роли на одном peer_id захватываются одновременно", False, str(exc))
finally:
    lock_tx.close()

print()
if fails:
    print(f"RESULT: FAIL {len(fails)}")
    sys.exit(1)
print("RESULT: ALL GREEN")
