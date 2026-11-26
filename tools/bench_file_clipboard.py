"""Замер скорости файла через буфер (rdpconn.fileclip), не через CF_UNICODETEXT.

send на одной машине кладёт сгенерированный файл заданного размера в буфер.
recv на другой — ждёт его появления (wait_for_new_file), принимает, сверяет
sha256 и пишет отчёт: время до первого байта, общее время, средняя и пиковая
скорость. Только принимающая сторона может честно мерить канал — то же
рассуждение, что у bench_ceiling_live.py: send_file() возвращается, как
только ОС приняла объект в буфер, реальная передача байт происходит позже
и не на этой стороне.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rdpconn import fileclip


def body_for(count: int) -> bytes:
    """Несжимаемый поток: иначе на неизвестно каком этапе (RDP, диск, сам
    формат) что-нибудь схлопнет повтор одного байта и цифра соврёт — тот же
    приём, что в bench_ceiling_live.py."""
    return secrets.token_bytes(count)


def _peak_rate(samples: list[tuple[float, int]], window: float) -> float:
    """Та же формула, что в bench_ceiling_live.py._peak_rate (см. её докстринг
    и комментарий про баг с фактическим разбросом вместо номинального окна —
    здесь сразу в исправленном виде). Не вынесено в общий модуль: tools/*.py
    в этом проекте самостоятельные, без импортов друг у друга."""
    if len(samples) < 2:
        return 0.0
    times = [t for t, _ in samples]
    sizes = [n for _, n in samples]
    full_span = times[-1] - times[0]
    if full_span < window:
        return sum(sizes) / full_span if full_span > 0 else 0.0
    left = 0
    total = 0
    best = 0.0
    for right in range(len(samples)):
        total += sizes[right]
        while times[right] - times[left] > window:
            total -= sizes[left]
            left += 1
        rate = total / window
        if rate > best:
            best = rate
    return best


def send_main(args: argparse.Namespace) -> int:
    os.makedirs(args.workdir, exist_ok=True)
    path = Path(args.workdir) / f"bench_{args.bytes}.bin"
    data = body_for(args.bytes)
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    print(f"кладу в буфер {args.bytes} байт, sha256={digest}", flush=True)
    t0 = time.perf_counter()
    fileclip.send_file(path)
    dt = time.perf_counter() - t0
    print(f"send_file() вернулась за {dt:.3f} с (это не канал — "
         "см. докстринг про то, почему честная сторона только приём)", flush=True)
    if args.report:
        Path(args.report).write_text(
            f"байт {args.bytes}\nsha256 {digest}\nsend_file() {dt:.3f} с\n",
            encoding="utf-8")
    return 0


def recv_main(args: argparse.Namespace) -> int:
    seq = fileclip.sequence_number()
    print(f"жду новый файл в буфере (seq={seq})...", flush=True)
    fileclip.wait_for_new_file(seq, poll_interval=args.poll_interval, timeout=args.timeout)
    entries = fileclip.list_clipboard_files()
    if not entries:
        print("в буфере нет файлов после сигнала о новом — гонка?", flush=True)
        return 1
    entry = entries[0]
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = Path(args.out_dir) / entry.name
    result = fileclip.recv_file(0, str(out_path), chunk=args.chunk,
                                formatetc_retries=args.formatetc_retries,
                                formatetc_retry_wait=args.formatetc_retry_wait)
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    avg = result.bytes_total / result.total_s if result.total_s > 0 else 0.0
    peak = _peak_rate(result.samples, args.peak_window)
    lines = [
        f"байт {result.bytes_total} из заявленных {entry.size}",
        f"время до первого байта {result.first_byte_s:.3f} с",
        f"всего {result.total_s:.3f} с  среднее {avg:.0f} Б/с",
        f"пик (окно {args.peak_window:g} с, чанк {args.chunk}) {peak:.0f} Б/с",
        f"выборок {len(result.samples)}",
        f"sha256 {digest}",
    ]
    text = "\n".join(lines) + "\n"
    print(text, flush=True)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("send", "recv"))
    parser.add_argument("--bytes", type=int, default=1_000_000,
                        help="размер файла для send (по умолчанию 1 МБ)")
    parser.add_argument("--workdir", default=".",
                        help="куда send пишет сгенерированный файл")
    parser.add_argument("--out-dir", default="bench_file_recv",
                        help="куда recv сохраняет принятый файл")
    parser.add_argument("--chunk", type=int, default=524288,
                        help="размер одного IStream::Read на recv (по умолчанию 512 КБ — "
                             "плато пропускной способности, см. память проекта)")
    parser.add_argument("--poll-interval", type=float, default=0.05,
                        help="как часто recv проверяет смену буфера, в секундах")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="сколько recv ждёт появления файла, в секундах")
    parser.add_argument("--peak-window", type=float, default=1.0,
                        help="окно для пиковой скорости в секундах")
    parser.add_argument("--formatetc-retries", type=int, default=15,
                        help="сколько раз повторить GetData(FileContents), "
                             "если редиректор ещё не готов сразу после "
                             "появления файла (см. docstring recv_file())")
    parser.add_argument("--formatetc-retry-wait", type=float, default=0.1,
                        help="пауза между повторами GetData(FileContents), в секундах")
    parser.add_argument("--report", default="",
                        help="куда записать текстовый отчёт (опционально)")
    args = parser.parse_args()
    return send_main(args) if args.mode == "send" else recv_main(args)


if __name__ == "__main__":
    sys.exit(main())
