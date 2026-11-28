"""Вторая часть эксперимента (gate_bulk_oleSetClipboard_hang.md, 28.11.2026):
первая попытка (chisty burst на хосте, без встречной записи с гостя) НЕ
воспроизвела зависание — передача отработала за 21 мс. Проверяем вторую,
более близкую к реальному трафику gate.py гипотезу: ДВУСТОРОННЯЯ пачка
(обе стороны пишут по очереди), не только односторонняя.

Гость (B): запускать этим скриптом.
Хост (A): tools/exp_burst_then_file.py, запущенный ПРИМЕРНО одновременно
          (не нужна точная синхронизация — важно, что обе стороны только
          что активно писали в тот же слот).
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rdpconn import clipboard as cb
from rdpconn import fileclip


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("out_dir")
    p.add_argument("--burst", type=int, default=20)
    p.add_argument("--burst-gap", type=float, default=0.05)
    p.add_argument("--timeout", type=float, default=190.0)
    args = p.parse_args()

    store = cb.WindowsClipboard()
    since_seq_before_burst = fileclip.sequence_number()
    print(f">> мой seq() до пачки: {since_seq_before_burst}", flush=True)
    print(f">> пачка из {args.burst} быстрых текстовых записей со СВОЕЙ стороны...", flush=True)
    t0 = time.monotonic()
    for i in range(args.burst):
        store.write(f"RDP1|B:{i}:guest-burst-{i}-" + "y" * 40)
        time.sleep(args.burst_gap)
    print(f">> пачка заняла {time.monotonic() - t0:.2f} с, жду файл от хоста "
         f"(timeout={args.timeout:g} с)...", flush=True)

    since_seq = fileclip.sequence_number()
    print(f">> мой seq() сразу перед wait_for_new_file: {since_seq}", flush=True)
    try:
        fileclip.wait_for_new_file(since_seq, timeout=args.timeout)
    except TimeoutError as exc:
        print(f">> ПРОВАЛ: {exc}", flush=True)
        return 1
    entries = fileclip.list_clipboard_files()
    os.makedirs(args.out_dir, exist_ok=True)
    for i, e in enumerate(entries):
        out_path = os.path.join(args.out_dir, e.name)
        result = fileclip.recv_file(i, out_path)
        rate = result.bytes_total / result.total_s if result.total_s > 0 else 0.0
        print(f">> УСПЕХ [{i}] {e.name}: {result.bytes_total} байт за {result.total_s:.3f} с "
             f"(первый байт {result.first_byte_s:.3f} с, {rate:.0f} Б/с)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
