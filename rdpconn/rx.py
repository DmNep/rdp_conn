"""Программа №2 (вторая сторона): слушает канал и отвечает на команды.

    python -m rdpconn.rx --peer-id B
    python -m rdpconn.rx --peer-id B --exec
    python -m rdpconn.rx --slot file:slot.txt --limit 1     # проверка без Windows

Каждый принятый CMD подтверждается ACK с тем же seq (docs/protocol.md 4). Кадр,
испорченный после чтения заголовка, получает NAK с причиной; кадр с нечитаемым
заголовком (seq неизвестен) молча отбрасывается — отвечать на него нечем.

Подтверждение — это «кадр дошёл», а не «команда выполнена»: исполнение здесь
по умолчанию выключено, канал односторонним делала именно эта молчаливость. По
умолчанию на CMD кроме ACK уходит NOTE с эхом текста, чтобы отправитель видел не
только доставку, но и то, какой текст реально доехал (кодовая страница сторон
могла разъехаться — эхо это показывает). С --exec команда исполняется локально,
а её вывод возвращается NOTE-кадрами.

Своих PING rx не заводит (см. app.Heartbeat.proactive): отправитель и так
обстреливает канал контрольными кадрами, а каждый PONG — уже признак жизни, так
что порог молчания отрабатывает и без наших кадров. Двусторонний пинг удваивает
записи в слот, а на лагающем RDP-редиректе это превращает heartbeat в шум,
который сам же и мешает обмену.

Остановка: --limit по числу кадров, --duration по времени. Без обоих слушает
вечно до Ctrl-C.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

from . import app, config, framing

# Потолок возвращаемого вывода. Кадр держит 240 байт, поэтому вывод и так идёт
# несколькими NOTE; без потолка одна `type big.log` заполнила бы буфер обмена на
# минуты и забил бы весь канал.
EXEC_OUTPUT_LIMIT = 4096


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rdpconn.rx",
        description="Слушать канал, печатать принятые кадры и отвечать на них.",
    )
    app.add_channel_args(p)
    p.add_argument("--limit", type=int, default=None,
                   help="остановиться после N кадров (по умолчанию — вечно)")
    p.add_argument("--duration", type=float, default=None,
                   help="сколько секунд слушать всего")
    p.add_argument("--no-reply", action="store_true",
                   help="не отвечать ACK/NAK (когда подтверждение шлёт другая программа)")
    p.add_argument("--no-echo", action="store_true",
                   help="не слать NOTE-эхо принятой команды")
    p.add_argument("--exec", dest="exec_", action="store_true",
                   help="исполнять принятую команду локально и вернуть вывод NOTE")
    p.add_argument("--exec-timeout", type=float, default=20.0,
                   help="сколько секунд ждать завершения команды (по умолчанию 20)")
    p.add_argument("--show-hex", action="store_true", help="печатать payload в hex")
    return p


def describe(frame, codec: str = config.CODEPAGE) -> str:
    payload = frame.payload
    if payload and len(payload) <= 48:
        if frame.type in (config.CMD, config.NOTE):
            return f"{frame} payload={payload.decode(codec, errors='replace')!r}"
        return f"{frame} payload={payload.hex()}"
    return str(frame)


def _send_exec_output(transport, frame, args, codec: str = config.CODEPAGE) -> None:
    """Исполнить команду и вернуть вывод NOTE-кадрами.

    Если команда не выполняется, возвращаем текст ошибки: пустой ответ
    неотличим от молчавшего канала.
    """
    text = frame.payload.decode(codec, errors="replace")
    try:
        # Без text=True: он декодирует вывод локальной ANSI-страницей (cp1251),
        # а cmd.exe пишет в OEM-странице консоли. Кириллица в выводе
        # превращалась бы в '?' ещё до попадания в кадр.
        proc = subprocess.run(text, capture_output=True, shell=True,
                              timeout=args.exec_timeout)
        raw = proc.stdout if proc.returncode == 0 else proc.stderr
        output = raw.decode(codec, errors="replace")[:EXEC_OUTPUT_LIMIT]
    except subprocess.TimeoutExpired:
        output = f"команда прервана по таймауту {args.exec_timeout:.0f} с"
    except Exception as exc:
        output = f"ошибка: {exc}"
    # Ограничение payload = 240 байт, поэтому разбиваем на NOTE-фразы.
    for i in range(0, len(output), config.MAX_PAYLOAD):
        chunk = output[i:i + config.MAX_PAYLOAD].encode(codec, errors="replace")
        fail = app.send_frame(transport, framing.build_frame(config.NOTE, frame.seq, chunk))
        if fail is None:
            app.say(f">> NOTE seq={frame.seq} len={len(chunk)}")
        else:
            app.warn(f"?? NOTE seq={frame.seq} не отправлен: {fail}")
            break


def handle(args, transport, frame, err, codec: str = config.CODEPAGE) -> bool:
    """Обработать один приём. True — засчитать событие (для --limit)."""
    if frame is not None:
        app.say(f"<< {describe(frame, codec)}")
        if args.show_hex:
            app.say(f"   hex={frame.payload.hex()}")
        # Подтверждаем только CMD: это то, что реально шлёт наша пара (tx).
        # ACK на ACK или ACK на чужой тип развёл бы бесконечный обмен, а
        # протокол требует ответа лишь кадрам, ждущим подтверждения.
        if not args.no_reply and frame.type == config.CMD:
            fail = app.send_frame(transport, framing.ack(frame.seq))
            if fail is None:
                app.say(f">> ACK seq={frame.seq}")
            else:
                app.warn(f"?? ACK seq={frame.seq} не отправлен: {fail}")
            # --exec заменяет эхо на запуск команды: вывод идёт NOTE-пакетами.
            if args.exec_:
                _send_exec_output(transport, frame, args, codec)
            elif not args.no_echo:
                fail = app.send_frame(transport, framing.echo(frame.seq, frame.payload))
                if fail is None:
                    app.say(f">> NOTE seq={frame.seq} (эхо)")
                else:
                    app.warn(f"?? NOTE seq={frame.seq} не отправлен: {fail}")
        return True
    if err is not None:
        app.warn(f"!! испорченный кадр: {err} (code={err.code:#04x}, seq={err.seq})")
        if not args.no_reply and err.seq is not None:
            fail = app.send_frame(transport, framing.nak(err.seq, err.code))
            if fail is None:
                app.warn(f">> NAK seq={err.seq} code={err.code:#04x}")
            else:
                app.warn(f"?? NAK seq={err.seq} не отправлен: {fail}")
        return True
    return False


def step(args, transport, hb, frame, err, codec: str = config.CODEPAGE) -> bool:
    """Обработать один приём вместе с heartbeat. True — засчитать событие.

    Порядок здесь важнее содержания: heartbeat обязан отрабатывать, не прерывая
    проход. Кадр уже в руках вызывающего, и любой выход из функции раньше
    handle() означал бы его выбрасывание — ровно так на живом буфере и терялась
    первая попытка CMD: PING успевал поспеть в тот же проход, что и команда, а
    continue в цикле уводил начало круга мимо обработки.

    Отдельная функция заодно делает это проверямым: раньше вся последовательность
    жила внутри while, и поймать нужный проход можно было только таймингом, то
    есть тест на этот случай получался плавающим.
    """
    # PING собеседника поглощается здесь: он служебный и в --limit событием не
    # считается, иначе heartbeat съедал бы лимит.
    if app.heartbeat_handle(hb, transport, frame):
        return False
    # Любой принятый кадр — признак жизни: собеседник, честно отвечающий NOTE,
    # жив, и объявлять его потерянным незачем.
    if frame is not None and hb.note():
        app.say(".. собеседник снова отвечает")
    app.heartbeat_tick(hb, transport)
    app.heartbeat_check(hb, transport)
    return handle(args, transport, frame, err, codec)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        channel = app.open_channel(args)
    except app.UsageError as exc:
        app.warn(str(exc))
        return 2

    app.say(f"канал: {channel.label}")
    app.peer_hint(args)

    transport = channel.transport
    codec = args.codepage
    timeout = app.wait_timeout(args)
    # Приёмник на PING отвечает, но своих не заводит: см. Heartbeat.proactive.
    hb = app.make_heartbeat(args, proactive=False)
    started = time.monotonic()
    received = 0
    try:
        while True:
            if args.limit is not None and received >= args.limit:
                break
            if args.duration is not None and time.monotonic() - started >= args.duration:
                break
            # Просыпаемся раньше таймаута, если пора пинговать: иначе PING
            # уходил бы только вместе с чужим кадром, то есть никогда.
            wait = hb.wait(timeout)
            if args.duration is not None:
                wait = max(0.001, min(wait, args.duration - (time.monotonic() - started)))
            frame, err = app.receive_frame(transport, wait)
            if step(args, transport, hb, frame, err, codec):
                received += 1
    except KeyboardInterrupt:
        app.say("")
    finally:
        channel.close()
    app.say(f"принято событий: {received}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
