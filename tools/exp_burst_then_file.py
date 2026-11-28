"""Изолированный эксперимент (gate_bulk_oleSetClipboard_hang.md, 28.11.2026):
проверить гипотезу, что интенсивный текстовый трафик того же слота
непосредственно перед переключением на файл мешает RDP-редиректу донести
смену буфера до получателя. Чистый fileclip, без gate.py вообще.

Хост (A): запускать этим скриптом с --role sender.
Гость (B): python -m rdpconn.fileclip watch <dir> --timeout 60
           (обычная команда, без изменений — приёмник ничего не знает
           про то, что было до анонса).
"""
import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rdpconn import clipboard as cb
from rdpconn import fileclip


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--burst", type=int, default=20, help="число быстрых текстовых записей перед файлом")
    p.add_argument("--burst-gap", type=float, default=0.05, help="пауза между записями пачки")
    p.add_argument("--size", type=int, default=49152, help="размер тестового файла в байтах")
    p.add_argument("--settle", type=float, default=0.0,
                   help="пауза между последней текстовой записью и файлом "
                        "(в gate.py это NUDGE_AFTER_S~0.94с; в первых двух "
                        "прогонах эксперимента было ~0с)")
    args = p.parse_args()

    store = cb.WindowsClipboard()
    print(f">> пачка из {args.burst} быстрых текстовых записей...", flush=True)
    t0 = time.monotonic()
    for i in range(args.burst):
        store.write(f"RDP1|A:{i}:burst-{i}-" + "x" * 40)
        time.sleep(args.burst_gap)
    print(f">> пачка заняла {time.monotonic() - t0:.2f} с, settle={args.settle:g}с, "
         f"потом переключаю на файл", flush=True)
    if args.settle > 0:
        time.sleep(args.settle)

    fd, path = tempfile.mkstemp(prefix="rdpconn-exp-burst-")
    with os.fdopen(fd, "wb") as f:
        f.write(os.urandom(args.size))
    t1 = time.monotonic()
    fileclip.send_files([path])
    print(f">> файл записан в буфер за {time.monotonic() - t1:.3f} с: {path} ({args.size} байт)", flush=True)
    print(">> жду 190 с, чтобы дать получателю шанс (или не шанс) заметить смену...", flush=True)
    deadline = time.monotonic() + 190
    last_seq = fileclip.sequence_number()
    print(f">> мой seq() сразу после записи: {last_seq}", flush=True)
    while time.monotonic() < deadline:
        time.sleep(5)
        seq = fileclip.sequence_number()
        print(f".. t={time.monotonic() - t1:.0f}с, мой seq()={seq}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
