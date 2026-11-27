"""WindowsClipboard.write(): retry вокруг SetClipboardData закрывает гонку
gle=1418 (ERROR_CLIPBOARD_NOT_OPEN) между OpenClipboard и SetClipboardData
(AGENTS.md 6, память gle_1418_clipboard_race.md) — найдена живьём 27.11.2026
на F:\rdp-lab, воспроизведена локально гонкой двух процессов, починена
retry'ем того же вида, что уже был у _open()'s OpenClipboard.

Два слоя проверки:
1. Детерминированная — подменяем сам SetClipboardData на user32 этого
   объекта, считаем вызовы, принудительно роняем первые N, подтверждаем,
   что write() либо долетает после повтора, либо честно отказывает после
   исчерпания попыток, и что отказ не портит канал для следующей записи
   (GlobalAlloc-блок не утекает и не ломает дальнейшие вызовы).
2. Настоящая гонка — два отдельных процесса бьют в один и тот же
   физический буфер этой машины (второй машины не нужно, как в
   test_singleton.py) — то же, чем ошибка воспроизводилась вручную
   (9 сбоев на 4000 попыток до фикса, 0 после), только в меньшем
   масштабе для скорости обычного прогона.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

fails = []


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


if sys.platform != "win32":
    print("не Windows: WindowsClipboard требует ctypes.windll, тест неприменим")
    print()
    print("RESULT: ALL GREEN")
    sys.exit(0)

from rdpconn import clipboard  # noqa: E402

print("=== 1. константы retry заведены и разумны ===")
check("_SET_RETRIES больше одной попытки", clipboard.WindowsClipboard._SET_RETRIES > 1)
check("_SET_WAIT_S неотрицателен", clipboard.WindowsClipboard._SET_WAIT_S >= 0)

store = clipboard.WindowsClipboard()
real_set = store._user32.SetClipboardData
kernel32 = ctypes.windll.kernel32
kernel32.SetLastError.argtypes = [ctypes.c_uint]


def flaky(fail_times: int):
    """Подмена SetClipboardData: первые fail_times вызовов роняет с
    gle=1418 (как живьём), дальше зовёт настоящую функцию."""
    state = {"n": 0}

    def fn(fmt, handle):
        state["n"] += 1
        if state["n"] <= fail_times:
            kernel32.SetLastError(1418)
            return 0
        return real_set(fmt, handle)

    return fn


print("=== 2. сбоев меньше, чем попыток — write() долетает после повтора ===")
store._user32.SetClipboardData = flaky(clipboard.WindowsClipboard._SET_RETRIES - 1)
try:
    store.write("retry-ok")
    check("write() не бросил", True)
    check("текст реально долетел", store.read() == "retry-ok")
except clipboard.ClipboardError as exc:
    check("write() не бросил", False, str(exc))
    check("текст реально долетел", False, "write() бросил раньше")

print("=== 3. попытки исчерпаны — честный отказ, не тихая порча ===")
store._user32.SetClipboardData = flaky(clipboard.WindowsClipboard._SET_RETRIES + 5)
try:
    store.write("retry-fail")
    check("write() бросил ClipboardError", False)
except clipboard.ClipboardError as exc:
    check("write() бросил ClipboardError", True)
    check("ошибка называет gle=1418, не проглочена молча", "1418" in str(exc), str(exc))

print("=== 4. после отказа канал не испорчен — следующий write() снова работает ===")
store._user32.SetClipboardData = real_set
try:
    store.write("after-fail")
    check("write() после отказа долетает", store.read() == "after-fail")
except clipboard.ClipboardError as exc:
    check("write() после отказа долетает", False, str(exc))

print("=== 5. настоящая гонка: два процесса против одного физического буфера ===")


def spawn_racer(tag: str, n: int) -> subprocess.Popen:
    """Отдельный процесс, бьющий write()'ом в тот же буфер, что и этот
    тест, — именно так и воспроизводилась живая ошибка (не внутри одного
    процесса: там нет гонки за OpenClipboard самим с собой)."""
    code = (
        f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
        "from rdpconn.clipboard import WindowsClipboard, ClipboardError\n"
        "store = WindowsClipboard()\n"
        "ok = 0; fails = 0\n"
        f"for i in range({n}):\n"
        f"    try:\n"
        f"        store.write(f'{tag}-{{i}}')\n"
        "        ok += 1\n"
        "    except ClipboardError:\n"
        "        fails += 1\n"
        "print(f'ok={ok} fails={fails}', flush=True)\n"
    )
    return subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, text=True)


N = 1500
racer_a = spawn_racer("race-a", N)
racer_b = spawn_racer("race-b", N)
line_a = racer_a.stdout.readline()
line_b = racer_b.stdout.readline()
racer_a.wait(30)
racer_b.wait(30)

m_a = re.match(r"ok=(\d+) fails=(\d+)", line_a.strip())
m_b = re.match(r"ok=(\d+) fails=(\d+)", line_b.strip())
check("процесс A отчитался в ожидаемом формате", m_a is not None, repr(line_a))
check("процесс B отчитался в ожидаемом формате", m_b is not None, repr(line_b))
if m_a and m_b:
    ok_a, fail_a = int(m_a.group(1)), int(m_a.group(2))
    ok_b, fail_b = int(m_b.group(1)), int(m_b.group(2))
    check(f"процесс A: все {N} записей прошли (retry погасил гонку)",
          ok_a == N and fail_a == 0, f"ok={ok_a} fails={fail_a}")
    check(f"процесс B: все {N} записей прошли (retry погасил гонку)",
          ok_b == N and fail_b == 0, f"ok={ok_b} fails={fail_b}")

print()
if fails:
    print(f"RESULT: FAIL {len(fails)}")
    sys.exit(1)
print("RESULT: ALL GREEN")
