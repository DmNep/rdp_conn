"""Прогон полного снимка через живой редирект.

host поднимает эхо на 127.0.0.1. guest идёт в прокси шлюза CONNECT-ом
на этот адрес и шлёт известное число байт. Шлюз на обеих сторонах уже
должен быть запущен.

Эхо потоковое: host отвечает тем же куском сразу, как его принял, не
дожидаясь всего тела. Без этого «туда и обратно» — единственное число,
которое можно измерить (эхо приходит одним куском только после того, как
host успел получить всё целиком), и оно не отличает время установления
канала после простоя от скорости самой передачи — то и другое тонет в одной
сумме. Потоковый эхо даёт на приёме реальную последовательность моментов
«ещё кусок пришёл», по которой считаются и время до первого байта, и пик.

Отправка тоже идёт кусками через send() (не sendall()): send эхо-канала
ограничен окном буфера (_WINDOW в gate.py), и как только буфер шлюза
заполняется, ОС придерживает запись в сокет — это и есть настоящее обратное
давление канала, а не local loopback instant-write, которым раньше мерили
«туда».

Выборки времени — через time.perf_counter(), не time.monotonic() (которым
везде в rdpconn/*.py мерят секунды и минуты, там разница не видна). Когда
финальный снимок приносит разом десятки килобайт, локальная раздача этого
куска гостю занимает миллисекунды, и на одной из машин стенда monotonic()
даёт на таком отрезке одно и то же значение по нескольку вызовов подряд —
пик тогда считался бы по нулевой длительности и показывал 0 Б/с вместо
настоящего всплеска. perf_counter() всегда берёт самый точный таймер из
доступных и такого не делает.
"""

from __future__ import annotations

import argparse
import hashlib
import socket
import sys
import threading
import time
from pathlib import Path


def body_for(count: int) -> bytes:
    """Несжимаемый поток: иначе редирект сожмёт повтор одного байта и цифра врёт."""
    raw = bytearray()
    index = 0
    while len(raw) < count:
        raw += hashlib.sha256(index.to_bytes(4, "little")).digest()
        index += 1
    return bytes(raw[:count])


def _read_exact(sock: socket.socket, count: int) -> bytes | None:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(min(65536, count - len(buf)))
        if not chunk:
            return None
        buf += chunk
    return buf


def serve(port: int, chunk: int, echo: bool = True, direction: str = "b2a",
         count: int = 0, report: Path | None = None, peak_window: float = 1.0) -> None:
    """Эхо потоком: кусок пришёл — кусок сразу ушёл назад, без накопления.

    Батчевое эхо (принять всё, потом отправить всё) не даёт гостю увидеть
    ничего раньше, чем придёт целиком весь ответ, — тогда «время до первого
    байта» и «пик» считать не по чему, оба слились бы в одно число с «туда и
    обратно». Порядок байт при этом не меняется: гость сравнивает то, что
    получил, с тем, что сам послал.

    echo=False — только приём, без ответа (направление b2a: guest шлёт, host
    принимает). Это единственная сторона в этом направлении, на которой
    «скорость передачи файла в одну сторону» можно честно измерить: приём не
    обогнать локальной буферизацией, байт долетает только после того, как
    реально пересёк канал (см. docstring client() про то же самое в обратном
    направлении). guest-овская сторона с этой ролью не справится — её send()
    может быть мгновенным ещё до того, как канал принял хоть байт.

    direction="a2b" — ровно наоборот: host сам становится отправителем (шлёт
    count байт сразу после accept, без какого-либо ожидания от guest), а
    честная приёмная сторона — guest (см. client(direction="a2b")). Нужно,
    чтобы не предполагать молча, что оба направления канала одинаковы —
    gate.py's dial_loop/listen_loop устроены не симметрично.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    srv.settimeout(180)
    label = "отправитель" if direction == "a2b" else ("эхо" if echo else "приёмник")
    print(f"{label} 127.0.0.1:{port}", flush=True)
    try:
        conn, _addr = srv.accept()
    except socket.timeout:
        print("никто не пришёл", flush=True)
        return
    accepted = time.perf_counter()
    with conn:
        conn.settimeout(180)
        if direction == "a2b":
            # Никакого протокола-рукопожатия не нужно: guest уже знает count
            # из своего --bytes, а host — отправитель, не ждёт от guest ничего.
            samples: list[tuple[float, int]] = []
            try:
                _send_timed(conn, body_for(count), chunk, samples)
            except OSError as exc:
                print(f"обрыв при отправке: {exc}", flush=True)
                return
            print(f"host отправил {count} байт (a2b, цифры не канальные — "
                 "настоящая сторона отчёта — guest)", flush=True)
            return
        got = _read_exact(conn, 4)
        if got is None:
            print("нет длины", flush=True)
            return
        count = int.from_bytes(got, "big")
        received = 0
        samples: list[tuple[float, int]] = []
        try:
            while received < count:
                piece = conn.recv(min(chunk, count - received))
                if not piece:
                    print(f"обрыв на {received} из {count}", flush=True)
                    return
                if echo:
                    conn.sendall(piece)
                samples.append((time.perf_counter(), len(piece)))
                received += len(piece)
        except OSError as exc:
            print(f"обрыв: {exc}", flush=True)
            return
        if echo:
            print(f"эхо вернул {count} байт потоком", flush=True)
            return
        first_byte = samples[0][0] - accepted if samples else -1.0
        total_s = samples[-1][0] - accepted if samples else 0.0
        avg_rate = count / total_s if total_s > 0 else 0.0
        peak = _peak_rate(samples, peak_window)
        lines = [
            f"байт {count}",
            f"время до первого байта {first_byte:.2f} с",
            f"приём {total_s:.2f} с  среднее {avg_rate:.0f} Б/с",
            f"пик приёма (окно {peak_window:g} с) {peak:.0f} Б/с",
        ]
        text = "\n".join(lines) + "\n"
        print(text, flush=True)
        if report is not None:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(text, encoding="utf-8")


def _send_timed(sock: socket.socket, data: bytes, chunk: int,
                samples: list[tuple[float, int]]) -> None:
    """send(), не sendall(): частичная отправка — уже сигнал, что канал придержал."""
    view = memoryview(data)
    pos = 0
    while pos < len(view):
        sent = sock.send(view[pos:pos + chunk])
        pos += sent
        samples.append((time.perf_counter(), sent))


def _peak_rate(samples: list[tuple[float, int]], window: float) -> float:
    """Наибольшая скорость (Б/с) в любом скользящем окне длиной window секунд.

    Это оценка пика, а не среднего по всей дистанции: средним размывается
    ровно то, что у буферного канала и так рваное — всплеск быстрой передачи
    и установление/простой между снимками видны по отдельности, только если
    не складывать их в одно число. Окна короче window от начала/в разрывах
    не считаются: на огрызке времени скорость — это шум, а не измерение.
    """
    if len(samples) < 2:
        return 0.0
    times = [t for t, _ in samples]
    sizes = [n for _, n in samples]
    full_span = times[-1] - times[0]
    if full_span < window:
        # Вся выборка короче окна: окну взяться неоткуда, честнее отдать
        # среднее по тому, что есть, чем молча вернуть 0 — короткий прогон
        # (сам тест быстрее заданного окна) не должен выглядеть как отсутствие
        # данных.
        return sum(sizes) / full_span if full_span > 0 else 0.0
    left = 0
    total = 0
    best = 0.0
    for right in range(len(samples)):
        total += sizes[right]
        while times[right] - times[left] > window:
            total -= sizes[left]
            left += 1
        # Делим на номинальную длину окна, а не на фактический разброс
        # times[right]-times[left]: цикл выше ужимает его ровно до <= window,
        # а не до window впритык, так что условие «разброс почти равен window»
        # почти никогда не выполняется на редких, неравномерно разнесённых
        # выборках — именно так пик раньше молча оставался на стартовом 0.
        rate = total / window
        if rate > best:
            best = rate
    return best


def client(proxy: str, port: int, count: int, report: Path,
          chunk: int, peak_window: float, echo: bool = True,
          direction: str = "b2a") -> int:
    """echo=False: только отправка, без ожидания ответа.

    Цифры этой стороны (отправка/пик отправки) при echo=False не доказывают
    скорость канала — send() может отработать мгновенно в локальный буфер ОС
    и буфер gate.py ещё до того, как канал принял хоть байт (см. _send_timed).
    Настоящее число для одной стороны — на host (serve(..., echo=False)):
    приём не обогнать локальной буферизацией.

    direction="a2b" — guest здесь приёмник, host (см. serve(direction="a2b"))
    сам шлёт count байт без приглашения. Та же логика «не обогнать канал»,
    что у host(echo=False), только зеркально: это честная сторона именно для
    направления A→B.
    """
    host, _, port_s = proxy.rpartition(":")
    sock = socket.create_connection((host or "127.0.0.1", int(port_s)), 15)
    sock.settimeout(180)
    started = time.perf_counter()
    request = (
        f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n\r\n"
    ).encode("ascii")
    sock.sendall(request)
    head = b""
    while b"\r\n\r\n" not in head and len(head) < 4096:
        piece = sock.recv(256)
        if not piece:
            break
        head += piece
    opened = time.perf_counter()
    if b"200" not in head.split(b"\r\n", 1)[0]:
        text = f"прокси не открыл туннель: {head[:200]!r}\n"
        report.write_text(text, encoding="utf-8")
        print(text, flush=True)
        return 1

    if direction == "a2b":
        samples: list[tuple[float, int]] = []
        got = bytearray()
        try:
            while len(got) < count:
                piece = sock.recv(min(chunk, count - len(got)))
                if not piece:
                    break
                got.extend(piece)
                samples.append((time.perf_counter(), len(piece)))
        except OSError as exc:
            text = f"обрыв при приёме: {exc}\n"
            report.write_text(text, encoding="utf-8")
            print(text, flush=True)
            return 1
        first_byte = samples[0][0] - opened if samples else -1.0
        total_s = samples[-1][0] - opened if samples else 0.0
        avg_rate = len(got) / total_s if total_s > 0 else 0.0
        peak = _peak_rate(samples, peak_window)
        lines = [
            f"байт {len(got)} из {count}",
            f"время до первого байта {first_byte:.2f} с",
            f"приём {total_s:.2f} с  среднее {avg_rate:.0f} Б/с",
            f"пик приёма (окно {peak_window:g} с) {peak:.0f} Б/с",
        ]
        text = "\n".join(lines) + "\n"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text, encoding="utf-8")
        print(text, flush=True)
        return 0 if len(got) == count else 1

    body = body_for(count)
    up_samples: list[tuple[float, int]] = []

    if not echo:
        _send_timed(sock, count.to_bytes(4, "big"), chunk, up_samples)
        _send_timed(sock, body, chunk, up_samples)
        sent = time.perf_counter()
        upload_s = sent - opened
        avg_rate = count / upload_s if upload_s > 0 else 0.0
        up_peak = _peak_rate(up_samples, peak_window)
        lines = [
            f"байт {count}",
            f"отправка (локально, не доказывает скорость канала) {upload_s:.2f} с"
            f"  среднее {avg_rate:.0f} Б/с  пик {up_peak:.0f} Б/с",
            "настоящая скорость одной стороны — в отчёте host (serve с --no-echo)",
        ]
        text = "\n".join(lines) + "\n"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text, encoding="utf-8")
        print(text, flush=True)
        return 0

    down_samples: list[tuple[float, int]] = []
    down_buf = bytearray()
    down_done = threading.Event()
    down_errors: list[str] = []

    def reader() -> None:
        # Отдельный поток: читать нужно уже во время отправки, иначе первый
        # кусок эха успеет осесть в приёмном буфере ОС до того, как мы начнём
        # его забирать, и «время до первого байта» соврёт в меньшую сторону.
        try:
            while len(down_buf) < count:
                piece = sock.recv(min(chunk, count - len(down_buf)))
                if not piece:
                    down_errors.append("обрыв при чтении ответа")
                    break
                down_buf.extend(piece)
                down_samples.append((time.perf_counter(), len(piece)))
        except OSError as exc:
            down_errors.append(str(exc))
        finally:
            down_done.set()

    threading.Thread(target=reader, daemon=True).start()

    _send_timed(sock, count.to_bytes(4, "big"), chunk, up_samples)
    _send_timed(sock, body, chunk, up_samples)
    sent = time.perf_counter()

    down_done.wait(180)
    done = time.perf_counter()

    back = bytes(down_buf) if not down_errors else None
    ok = back is not None and back == body
    first_byte = (down_samples[0][0] - opened) if down_samples else -1.0
    upload_s = sent - opened
    total_s = done - started
    avg_rate = count / upload_s if upload_s > 0 else 0.0
    up_peak = _peak_rate(up_samples, peak_window)
    down_peak = _peak_rate(down_samples, peak_window)

    lines = [
        f"байт {count}",
        f"туннель {opened - started:.2f} с (локально, не через канал)",
        f"отправка {upload_s:.2f} с  среднее {avg_rate:.0f} Б/с",
        f"время до первого байта ответа {first_byte:.2f} с",
        f"туда и обратно {total_s:.2f} с",
        f"пик отправки (окно {peak_window:g} с) {up_peak:.0f} Б/с",
        f"пик приёма (окно {peak_window:g} с) {down_peak:.0f} Б/с",
        f"эхо {'ДА' if ok else 'НЕТ'}" + ("" if ok else f" ({'; '.join(down_errors) or 'не совпало'})"),
    ]
    text = "\n".join(lines) + "\n"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(text, encoding="utf-8")
    print(text, flush=True)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("host", "guest"))
    parser.add_argument("--port", type=int, default=18079)
    parser.add_argument("--proxy", default="127.0.0.1:8079")
    parser.add_argument("--bytes", type=int, default=262144)
    parser.add_argument("--report", default="")
    parser.add_argument("--chunk", type=int, default=4096,
                        help="размер одного send()/recv() для выборки (по умолчанию 4096)")
    parser.add_argument("--peak-window", type=float, default=1.0,
                        help="окно для пиковой скорости в секундах (по умолчанию 1.0)")
    parser.add_argument("--no-echo", action="store_true",
                        help="только одна сторона: host лишь принимает, guest лишь "
                             "шлёт — измерение скорости в одну сторону, не туда-обратно "
                             "(настоящая цифра — в отчёте host, см. docstring serve())")
    parser.add_argument("--direction", choices=("b2a", "a2b"), default="b2a",
                        help="b2a (по умолчанию) — guest шлёт host; a2b — host шлёт "
                             "guest. Честная цифра всегда у приёмника, не у "
                             "отправителя (см. docstring)")
    args = parser.parse_args()
    echo = not args.no_echo
    if args.mode == "host":
        report = Path(args.report) if args.report else None
        serve(args.port, args.chunk, echo=echo, direction=args.direction,
             count=args.bytes, report=report, peak_window=args.peak_window)
        return 0
    if not args.report:
        print("guest просит --report", flush=True)
        return 2
    return client(args.proxy, args.port, args.bytes, Path(args.report),
                 args.chunk, args.peak_window, echo=echo, direction=args.direction)


if __name__ == "__main__":
    sys.exit(main())
