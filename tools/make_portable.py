"""Собрать архив для переноса на машину без интернета.

В отличие от звукового предшественника, колёс тут нет: у канала нулевые
зависимости, поэтому на второй машине нечего ставить — достаточно Python 3.10+
и PYTHONPATH. Архив обязан оставаться маленьким именно поэтому; если в него
когда-нибудь попадёт whl, значит зависимость появилась и это надо обсуждать, а
не молча тащить.

venv не переносим: пути в нём абсолютные, на другой машине он не заработает.
"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "rdp_conn_portable.zip")

FILES = ("rdpconn", "tools", "docs")
EXTRA = ("README.md", "AGENTS.md", "LICENSE", "pyproject.toml")

INSTALL = r"""ПЕРЕНОС НА МАШИНУ БЕЗ ИНТЕРНЕТА (Windows)

Зависимостей нет: ставить нечего, internet на второй машине не нужен.
Нужен только Python 3.10+ (проверить: py --version).

1. Распакуй архив, например в D:\rdp_conn

2. Проверка без второй стороны (одной машиной, слот в файле):
   cd /d D:\rdp_conn
   set PYTHONPATH=%CD%
   py -m rdpconn rx --help

3. Приёмник ВНУТРИ RDP-сессии (сторона B):
   cd /d D:\rdp_conn
   set PYTHONPATH=%CD%
   py -m rdpconn rx --peer-id B --exec --hotkey ctrl+alt+f9

4. Отправитель на машине с запущенным mstsc (сторона A):
   cd /d D:\rdp_conn
   set PYTHONPATH=%CD%
   py -m rdpconn tx --peer-id A --repl

МЕТКА СТОРОНЫ ОБЯЗАТЕЛЬНА, а не по вкусу. Без --peer-id метка случайная, и
каждый перезапуск стороны выступает под новым id: старые ячейки остаются в
слоте чужим наследием, а собеседник теряет возможность сопоставить кадры
одного конца до и после перезапуска. Программа предупреждает об этом на старте.

НА ПРИЁМНИКЕ ДЕРЖИ РОВНО ОДИН rx. Два приёмника с одним --peer-id дают лавину
подтверждений на каждый кадр: обмен визуально работает, но тонет в повторе.
Признак -- шторм ">> PONG" и удвоенные "<< ACK" в логе отправителя. Проверь:
    tasklist /fi "IMAGENAME eq python.exe"

ЕСЛИ ПРИЁМНИК НЕ ОТВЕЧАЕТ
   - Редирект буфера обмена в mstsc включён: Local Resources ->
     Local devices and resources -> Clipboard. Без него слота между машинами
   - На отправителе в тексте слота должен появляться текст "RDP1|A:...".
     Проверить: Ctrl+V в Блокнот сразу после отправки.
   - --peer-id на обеих сторонах задан и различается.
   - Проверь сам слот без второй стороны:
     py -m rdpconn tx --slot file:slot.txt --peer-id A --wait 0.5 "hostname"
     (подтверждения не будет, но кадр в файл лечь обязан)

НЕ СТАВЬ --ping МЕНЬШЕ 10. Редирект буфера в RDP сам по себе запаздывает на
единицы-десятки секунд, и живой канал начал бы объявляться потерянным. Порог
молчания -- --ping, умноженный на 3; по умолчанию 25 x 3 = 75 с.

ЕСЛИ ТЫ КОПИРУЕШЬ СВОЁ
   Буфер один. Приёмник замечает человеческую копию и сообщает о ней, но обмен
   не останавливает: иначе в занятом буфере он не смог бы ответить
   подтверждением, и обмен встал бы совсем. В слоте живут последние кадры обеих
   сторон, поэтому подтверждение и ответ не затирают команду.
   Хочешь отдать буфер целиком -- жми --hotkey (например ctrl+alt+f9): обмен
   встанет полностью, повторное нажатие продолжит. Без клавиши занятый слот
   освобождается сам через --pause-timeout (по умолчанию 60 с). Копировать
   ничего не будешь -- добавь --no-auto-pause.

КОДИРОВКА
   Канал -- это Windows<->Windows, обе половины здесь cmd.exe, поэтому текст
   идёт в cp866 и кириллица сохраняется. Если у тебя другая страница --
   --codepage на обеих сторонах одинаково. Признак разъезда -- вместо кириллицы
   вопросительные знаки ещё до отправки.

ПРОВЕРКА НА ОДНОЙ МАШИНЕ (без второй стороны)
   cd /d D:\rdp_conn
   set PYTHONPATH=%CD%
   py tools\test_frames.py
   py tools\test_clipboard.py
   py tools\test_heartbeat.py
   py tools\test_duplex.py
   Каждая завершается строкой "RESULT: ALL GREEN". Полная проверка логики:
   кадр и CRC, пауза и возврат слота, чужие копии, heartbeat и сквозной обмен
   двумя процессами через один слот.

ОГРАНИЧЕНИЕ
   Кадру нужно быть текстом, поэтому он кодируется в base64: 240 байт кадра
   дают ~330 символов в буфере. Для консоли этого с запасом.

ЧИТАТЬ ДАЛЬШЕ
   README.md -- быстрый старт и флаги
   docs\protocol.md -- кадр, типы, цикл обмена, коды ошибок
"""


def add_dir(zf: zipfile.ZipFile, rel: str) -> int:
    """Положить каталог целиком, пропуская байт-код и рабочие файлы."""
    base = os.path.join(ROOT, rel)
    n = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "tmp")]
        for name in sorted(filenames):
            if name.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dirpath, name)
            arc = os.path.relpath(full, ROOT).replace("\\", "/")
            zf.write(full, arc)
            n += 1
    return n


def main() -> int:
    missing = [p for p in EXTRA if not os.path.exists(os.path.join(ROOT, p))]
    if missing:
        print("НЕТ ФАЙЛОВ:", ", ".join(missing))
        return 1

    if os.path.exists(OUT):
        os.remove(OUT)
    total = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in FILES:
            n = add_dir(zf, rel)
            print(f"  {rel}: {n} файлов")
            total += n
        for rel in EXTRA:
            zf.write(os.path.join(ROOT, rel), rel.replace("\\", "/"))
            print(f"  {rel}: 1 файл")
            total += 1
        zf.writestr("INSTALL.txt", INSTALL)
        total += 1

    size = os.path.getsize(OUT)
    print()
    print(f"Архив: {OUT}")
    print(f"Файлов: {total}, размер: {size / 1024:.1f} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())