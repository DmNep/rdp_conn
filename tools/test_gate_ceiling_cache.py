"""Потолок ячеек gate.Ceiling переживает перезапуск процесса (docs/roadmap.md,
пункт 4): следующий запуск того же peer_id стартует не с GATE_CELLS_MIN
холодно, а с последнего значения, подтверждённого настоящей передачей.

Активный upfront-замер потолка (как было бы для полной "единой калибровки"
из пункта 3) здесь сознательно не сделан — gate/coder не отвечают PONG на
пробный PING (их приёмный цикл разбирает только свой magic-кадр, см.
apply_frames()), и такой замер либо никогда не отвечал бы, либо тратил бы
секунды на пустые попытки при каждом старте. Кэш последнего реального
потолка — то, что можно сделать безопасно, без новой двусторонней
синхронизации.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdpconn import gate

fails = []


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="rdpconn_ceiling_cache_")
old_dir = gate._CEILING_DIR
gate._CEILING_DIR = tmp
try:
    print("=== 1. без кэша -- холодный старт с GATE_CELLS_MIN ===")
    check("нет записи -> None", gate._load_cached_ceiling("A") is None)
    c = gate.Ceiling(peer_id="A")
    check("Ceiling() без кэша стартует с минимума", c.cells == gate.GATE_CELLS_MIN, c.cells)

    print("=== 2. _save/_load -- round-trip ===")
    gate._save_cached_ceiling("A", 52)
    check("значение сохранилось и прочиталось", gate._load_cached_ceiling("A") == 52)

    print("=== 3. Ceiling() подхватывает кэш того же peer_id ===")
    c2 = gate.Ceiling(peer_id="A")
    check("стартует с кэша, не с минимума", c2.cells == 52, c2.cells)

    print("=== 4. разные peer_id не делят кэш ===")
    check("у 'B' своего кэша ещё нет", gate._load_cached_ceiling("B") is None)
    c3 = gate.Ceiling(peer_id="B")
    check("Ceiling() для 'B' стартует с минимума", c3.cells == gate.GATE_CELLS_MIN, c3.cells)

    print("=== 5. испорченное/вне диапазона значение -- кэш игнорируется ===")
    gate._save_cached_ceiling("C", gate.GATE_CELLS_MAX + 100)
    check("значение вне [MIN, MAX] не доверяется",
         gate._load_cached_ceiling("C") is None)
    c4 = Path(gate._ceiling_cache_path("D"))
    c4.parent.mkdir(parents=True, exist_ok=True)
    c4.write_text("не json вовсе", encoding="utf-8")
    check("битый json не роняет _load_cached_ceiling",
         gate._load_cached_ceiling("D") is None)

    print("=== 6. _raise()/_drop() реально пишут кэш, не только меняют self.cells ===")
    c5 = gate.Ceiling(peer_id="E")
    c5.need = {1: 10}
    c5.armed = True
    c5._raise()
    check("после _raise() кэш обновлён",
         gate._load_cached_ceiling("E") == c5.cells, gate._load_cached_ceiling("E"))

    c6 = gate.Ceiling(peer_id="F")
    c6.armed = True
    c6.nudge_seen = True
    c6.need = {1: 10}
    c6._drop()
    check("после первой потери кэш обновлён (ступенью ниже, не сразу на минимум)",
         gate._load_cached_ceiling("F") == c6.cells, (gate._load_cached_ceiling("F"), c6.cells))
finally:
    gate._CEILING_DIR = old_dir
    shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print(f"RESULT: FAIL {len(fails)}")
    sys.exit(1)
print("RESULT: ALL GREEN")
