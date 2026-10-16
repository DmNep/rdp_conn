"""Программа №1 (сторона клиента): отправляет команды и читает ответ.

    python -m rdpconn.tx --peer-id A "hostname"
    python -m rdpconn.tx --peer-id A --repl
    python -m rdpconn.tx --slot file:slot.txt --peer-id A "hostname"   # без Windows

Полудуплекс соблюдает протокол: кадр ушёл, затем ждём ACK. Без подтверждения
команда неотличима от потерянной, поэтому --no-ack только для отладки канала.

Канал двусторонний, поэтому подтверждение — не единственный ожидаемый кадр.
Ответная сторона печатает результат NOTE и отвечает на heartbeat PONG; всё это
печатается и попытку не тратит: посторонний ответ — часть разговора, а не его
отсутствие. Ретранслируем мы только тишину и NAK.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from queue import Empty, Queue

from . import app, config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rdpconn.tx",
        description="Отправить команду по каналу, дождаться подтверждения и ответа.",
    )
    app.add_channel_args(p)
    p.add_argument("command", nargs="*", help="текст команды (можно несколько)")
    p.add_argument("--repl", action="store_true", help="читать команды с stdin")
    p.add_argument("--no-ack", action="store_true",
                   help="не ждать ACK (отладка канала, надёжность ниже)")
    p.add_argument("--retries", type=int, default=config.MAX_RETRY,
                   help=f"сколько раз повторить без подтверждения "
                        f"(по умолчанию {config.MAX_RETRY})")
    p.add_argument("--reply-wait", type=float, default=0.8,
                   help="сколько секунд ждать продолжение ответа после ACK "
                        "(по умолчанию 0.8; для --exec на другой стороне нужно больше)")
    return p


def show_reply(frame, codec: str = config.CODEPAGE) -> None:
    """Напечатать ответный текстовый кадр (NOTE).

    Ответная сторона шлёт вывод в той же кодировке, что мы шлём команду.
    Нерасшифруемое показываем hex: молча проглотить ответ — значит потерять его
    без единого признака.
    """
    try:
        shown = frame.payload.decode(codec)
    except UnicodeDecodeError:
        shown = frame.payload.hex()
    app.say(f"<< {frame.type_name} seq={frame.seq} {shown!r}")


def drain_reply(transport, wait: float, hb, codec: str = config.CODEPAGE) -> None:
    """Дочитать ответ, пришедший сразу за ACK.

    Порядок ответов таков, что ACK уходит раньше вывода: подтверждение нужно
    даже если исполнение зависло. Выход по первому ACK оставил бы NOTE с выводом
    в очереди транспорта, и тот припечатался бы к следующей команде. Ждём
    idle-паузу после каждого кадра, чтобы собрать пачку NOTE длинного вывода, а
    не только первый.
    """
    while True:
        frame, err = app.receive_frame(transport, wait)
        if frame is None:
            if err is not None:
                app.warn(f"?? ответ испорчен ({err}, code={err.code:#04x})")
            return
        if app.heartbeat_handle(hb, transport, frame):
            continue
        if frame.type in config.REPLY_TYPES:
            show_reply(frame, codec)
            continue
        # Всё остальное (чужой ACK/NAK, повтор) не наш ответ: проглотить его
        # нельзя, но и слать команду второй раз из-за него нельзя.
        app.say(f"<< {frame}")
        return


def send_one(args, transport, counter, text: str, hb, codec: str = config.CODEPAGE) -> bool:
    """Один обмен: CMD, затем ожидание ACK с тем же seq. True — подтверждено.

    Номер берётся один раз, до цикла попыток. Ретрансмиссия обязана повторять
    тот же seq (docs/protocol.md 4): сторона, уже исполнившая команду, на повтор
    с тем же номером отдаёт закешированный ответ, а не исполняет её второй раз.
    Новый номер на повторе был бы второй командой.
    """
    timeout = app.wait_timeout(args)
    seq = counter.take()
    for attempt in range(1, args.retries + 1):
        seq, fail = app.send_command(transport, counter, text, seq=seq, codec=codec)
        if fail is not None:
            app.warn(f"?? CMD seq={seq} не отправлен: {fail}")
            continue
        app.say(f">> CMD seq={seq} {text!r} (попытка {attempt})")
        if args.no_ack:
            return True
        # Ждём именно ACK seq. Ответные NOTE/PONG печатаются и ожидание не
        # прерывают: попытку тратят только тишина, испорченный кадр и NAK.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            left = max(0.05, deadline - time.monotonic())
            frame, err = app.receive_frame(transport, left)
            if err is not None:
                app.warn(f"?? подтверждение испорчено ({err}, code={err.code:#04x})")
                break
            if frame is None:
                app.warn(f".. тишина {timeout:.2f} с, подтверждения нет")
                break
            # PING собеседника обязан получить PONG даже посреди ожидания ACK:
            # иначе он объявит нас мёртвыми ровно тогда, когда мы ждём ответа.
            if app.heartbeat_handle(hb, transport, frame):
                continue
            if frame.type == config.ACK and frame.seq == seq:
                app.say(f"<< ACK seq={frame.seq}")
                drain_reply(transport, args.reply_wait, hb, codec)
                return True
            if frame.type == config.NAK and frame.seq == seq:
                # Причина — байт NAK_* в payload, поэтому показываем payload как
                # есть: угадывать по длине здесь значит молча потерять причину.
                app.warn(f"<< NAK seq={frame.seq} payload={frame.payload.hex()} "
                         f"— не подтверждён, повторю")
                break
            if frame.type in config.REPLY_TYPES:
                show_reply(frame, codec)
                continue
            if frame.type == config.PONG:
                app.say(f"<< PONG seq={frame.seq}")
                continue
            app.warn(f"?? ожидался ACK seq={seq}, получен {frame}")
            break
    return False


def stdin_lines():
    """Строки stdin в очередь, читаемые фоновым потоком.

    Прямой input() останавливает весь цикл: пока человек думает, heartbeat не
    работает, и живой канал выглядит для собеседника потерянным. Отдельный поток
    возвращает управление циклу, а очередь — единственная безопасная передача
    между потоками.
    """
    queue = Queue()

    def reader() -> None:
        for line in sys.stdin:
            queue.put(line.rstrip("\n"))
        queue.put(None)  # EOF

    threading.Thread(target=reader, daemon=True).start()
    return queue


def idle(args, transport, hb) -> None:
    """Пауза между командами: поддержать heartbeat, не занимая ввод.

    Без этого молчащий REPL выглядел бы для собеседника как обрыв, а его PING
    копились бы в транспорте до следующей команды.
    """
    if not hb.enabled:
        return
    frame, err = app.receive_frame(transport, hb.wait(0.2))
    if err is not None:
        app.warn(f"?? кадр испорчен ({err}, code={err.code:#04x})")
        return
    if app.heartbeat_handle(hb, transport, frame):
        return
    if frame is not None:
        hb.note()
        app.say(f"<< {frame}")
    app.heartbeat_tick(hb, transport)
    app.heartbeat_check(hb, transport)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.command and not args.repl:
        app.warn("нужна команда в аргументах или --repl")
        return 2
    try:
        channel = app.open_channel(args)
    except app.UsageError as exc:
        app.warn(str(exc))
        return 2

    app.say(f"канал: {channel.label}")
    app.peer_hint(args)

    transport = channel.transport
    counter = app.SeqCounter()
    codec = args.codepage
    hb = app.make_heartbeat(args)
    failures = 0
    try:
        for text in args.command:
            if not send_one(args, transport, counter, text, hb, codec):
                failures += 1
            idle(args, transport, hb)
        if args.repl:
            app.say("tx> (команда, .q — выход)")
            lines = stdin_lines()
            while True:
                # Пауза в ожидании ввода — тоже часть работы канала: без
                # heartbeat собеседник счёл бы наш REPL обрывом.
                idle(args, transport, hb)
                try:
                    line = lines.get(timeout=0.05)
                except Empty:
                    continue
                if line is None:
                    app.say("")
                    break
                if line.strip() in (".q", ".quit", ".exit"):
                    break
                if not line.strip():
                    continue
                if not send_one(args, transport, counter, line, hb, codec):
                    failures += 1
    finally:
        channel.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())