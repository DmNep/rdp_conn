"""Потолок снимка без буфера: ступени, потеря, вторая потеря, пауза.

Ceiling() здесь без peer_id (как и было до кэша потолка, docs/roadmap.md
пункт 4) — а значит, без изоляции он читал бы и писал бы общий файл
%TEMP%\\rdpconn-ceiling\\no-peer-id.json: собственный прошлый прогон этого
же скрипта («выше 58 не поднимается» поднимает потолок до 58 и сохраняет
его) иначе ломал бы следующий запуск, стартуя не с 40, а с 58 — ровно так
один раз и сломалось при живой проверке 27.11.2026. Подменяем каталог
кэша на временный на время теста.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdpconn import gate  # noqa: E402
from rdpconn.gate import (  # noqa: E402
    GATE_CELLS_MAX,
    GATE_CELLS_MIN,
    Ceiling,
    text_limit,
)

gate._CEILING_DIR = tempfile.mkdtemp(prefix="rdpconn_test_ceiling_cache_")


class Pipe:
    def __init__(self, sid: int, base: int, offered: int) -> None:
        self.sid = sid
        self.base = base
        self.offered = offered
        self.to_peer = b""


class Transport:
    def __init__(self) -> None:
        self.on_nudge = None


def main() -> int:
    failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failed
        if not ok:
            failed += 1
        print(("OK  " if ok else "FAIL ") + name + (f"  {detail}" if detail and not ok else ""))

    check("40 ячеек — 14000 символов", text_limit(40) == 14000)
    check("58 ячеек больше 40", text_limit(58) > text_limit(40))
    check("символы растут ровно с ячейками", text_limit(46) == 14000 * 46 // 40)

    ceiling = Ceiling()
    pipe = Pipe(1, 0, 100)
    pipes = {1: pipe}
    transport = Transport()
    ceiling.arm(pipes, transport)
    check("полный снимок ждёт подтверждения", ceiling.armed)
    ceiling.observe(pipes)
    check("без повтора потеря не считается", ceiling.armed and ceiling.cells == 40)
    pipe.base = 100
    ceiling.observe(pipes)
    check("подтверждение поднимает на 6", ceiling.cells == 46, str(ceiling.cells))

    ceiling.arm({1: Pipe(1, 0, 50)}, transport)
    ceiling.nudge_seen = True
    ceiling.observe({1: Pipe(1, 0, 50)})
    check("повтор без подтверждения снимает 6", ceiling.cells == 40, str(ceiling.cells))
    check("на сорока подъём закрыт", ceiling.allow is False)

    ceiling.allow = True
    ceiling.cells = 52
    ceiling.strikes = 0
    ceiling.arm({1: Pipe(1, 0, 10)}, transport)
    ceiling.nudge_seen = True
    ceiling.observe({1: Pipe(1, 0, 10)})
    check("первая потеря с 52 на 46", ceiling.cells == 46 and ceiling.strikes == 1)
    ceiling.arm({1: Pipe(1, 0, 10)}, transport)
    ceiling.nudge_seen = True
    ceiling.observe({1: Pipe(1, 0, 10)})
    check("вторая потеря сразу на 40", ceiling.cells == GATE_CELLS_MIN and ceiling.allow is False)

    ceiling.allow = True
    ceiling.cells = 40
    ceiling.strikes = 0
    for _ in range(5):
        pending = Pipe(1, 0, 20)
        ceiling.arm({1: pending}, transport)
        pending.base = 20
        ceiling.observe({1: pending})
    check("выше 58 не поднимается", ceiling.cells == GATE_CELLS_MAX, str(ceiling.cells))

    ceiling.allow = False
    ceiling.armed = False
    ceiling.quiet_since = time.monotonic() - 3
    ceiling.observe({})
    check("после паузы лестницу можно снова", ceiling.allow is True)

    print("RESULT", "FAIL" if failed else "ALL GREEN")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(gate._CEILING_DIR, ignore_errors=True)
    sys.exit(code)
