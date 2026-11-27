"""Информационная проверка штатного канала при старте.

Буфер обмена — резервный канал по смыслу проекта (AGENTS.md 1): он нужен
ровно тогда, когда интернета и нормального RDP-канала не осталось. Бывает,
что это не так — интернет есть, а человек не заметил, или mstsc поднял
редирект диска, — и тогда вообще не обязательно упираться в буфер.

Эта проверка только сообщает об этом один раз при старте и не переключает
канал сама: решение использовать штатный путь вместо буфера — решение
владельца, не программы (docs/roadmap.md, пункт 2). Разворачивать его в
боевой канал (как это делает `gate`) здесь не её задача.

Обе проверки — короткие (доли секунды) и односторонние, без координации со
второй стороной. Это не то же самое, что `tools/rdp_disk_probe.py`: тот
даёт окончательный ответ (байты реально прошли туда-обратно через диск),
но для этого нужен человек на обеих сторонах и десятки секунд ожидания —
никакой автоматический старт себе такого позволить не может. Здесь —
только первый, быстрый признак: «\\tsclient хотя бы откликается», не
«редирект определённо работает».
"""

from __future__ import annotations

import os
import socket
import threading

# Один резолвер, не несколько: проверка должна иметь предсказуемый худший
# случай. Несколько последовательных проб суммируют свои таймауты именно
# тогда, когда интересно — когда сети действительно нет (обычный случай
# для этого канала), а не тогда, когда она есть и первая проба уже ответила.
_INTERNET_PROBE = ("1.1.1.1", 443)
_PROBE_TIMEOUT_S = 0.8


def has_internet(probe=_INTERNET_PROBE, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """Один короткий TCP-коннект. Не отвечает "сеть определённо жива",
    только "хоть что-то снаружи отозвалось за разумное время"."""
    try:
        with socket.create_connection(probe, timeout=timeout):
            return True
    except OSError:
        return False


def _listdir_with_timeout(path: str, timeout: float):
    """os.listdir(), но не дольше timeout.

    \\tsclient на машине без редиректа не отказывает мгновенно — он может
    молчать секундами (тот же эффект уже учтён в tools/rdp_disk_probe.py's
    listing()). Без таймаута единственная проверка штатного канала сама
    стала бы самым медленным шагом запуска.
    """
    box: dict = {}

    def run() -> None:
        try:
            box["ok"] = os.listdir(path)
        except OSError as exc:
            box["err"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive() or "err" in box:
        return None
    return box.get("ok", [])


def has_rdp_disk(root: str = r"\\tsclient", timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """\\tsclient отзывается вообще. См. модульный docstring: не окончательный
    ответ про редирект, только его первый признак."""
    return _listdir_with_timeout(root, timeout) is not None


def startup_hint(timeout: float = _PROBE_TIMEOUT_S) -> str | None:
    """Строка для печати при старте, если штатный путь похоже есть, иначе
    None. Интернет и диск проверяются одновременно (двумя потоками), чтобы
    общая задержка не росла с числом проверок — другого способа держать
    единый бюджет задержки при двух независимых сетевых операциях нет.
    """
    result: dict[str, bool] = {}

    def check_internet() -> None:
        result["internet"] = has_internet(timeout=timeout)

    def check_disk() -> None:
        result["disk"] = has_rdp_disk(timeout=timeout)

    threads = [threading.Thread(target=check_internet, daemon=True),
               threading.Thread(target=check_disk, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 0.3)

    found = []
    if result.get("internet"):
        found.append("похоже, есть выход в интернет")
    if result.get("disk"):
        found.append(r"\\tsclient отзывается (редирект диска может быть включён)")
    if not found:
        return None
    return (
        ".. штатный путь, возможно, тоже доступен: " + "; ".join(found) + ". "
        "Канал всё равно идёт через буфер, как и раньше (переключение — "
        "решение владельца, не автоматика; окончательно проверить диск — "
        "tools/rdp_disk_probe.py)"
    )
