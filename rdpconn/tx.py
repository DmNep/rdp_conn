"""Программа №1 (сторона клиента): отправляет команды и читает ответ.

    python -m rdpconn.tx --peer-id A "hostname"
    python -m rdpconn.tx --peer-id A --repl
    python -m rdpconn.tx --slot file:slot.txt --peer-id A "hostname"   # без Windows

Полудуплекс соблюдает протокол: кадр ушёл, затем ждём ACK. Короткие команды,
которые уже ждут в очереди, делят один снимок: пауза редиректа платится за
запись, а не за каждый кадр. Без подтверждения команда неотличима от
потерянной, поэтому --no-ack только для отладки канала.

Канал двусторонний, поэтому подтверждение — не единственный ожидаемый кадр.
Чужой NOTE и PONG печатаются и попытку не тратят. NOTE с тем же seq, что у
этой команды, попытку закрывает: ответ уже пришёл, и ждать отдельно
потерянный ACK значило бы послать команду ещё раз. Ретранслируем мы только
тишину и NAK на наш seq.

Команда длиннее одного кадра режется до сборки кадра (rdpconn.segment) и
собирается на приёмнике целиком. Кадр от этого не растёт.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from queue import Empty, Queue

from . import app, config, framing, segment, tune
from .clipboard import SNAPSHOT_MAX


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


def _back(hb) -> None:
    """Кадр собеседника снимает объявленный обрыв.

    Пока канал считается потерянным, свои PING не уходят. Если не засчитать
    обычный ACK или NOTE, строка «снова отвечает» не появится никогда:
    собеседник, который только исполняет команды, своих PING не шлёт.
    """
    if hb.note():
        app.say(".. собеседник снова отвечает")


class _Reader:
    """Приём с отложенными кадрами следующих кусков.

    Куски одной команды уходят одной записью буфера, и приёмник может
    ответить на следующий кусок, пока мы ещё дочитываем предыдущий.
    Такой кадр нельзя выбросить и нельзя оставить в общем приёме: следующий
    кусок иначе будет ждать подтверждение, которое уже прошло мимо.
    """

    def __init__(self, transport) -> None:
        self.transport = transport
        self._now = []
        self._later = []

    def receive(self, timeout: float):
        if self._now:
            return self._now.pop(0), None
        return app.receive_frame(self.transport, timeout)

    def park(self, frame) -> None:
        self._later.append(frame)

    def release(self) -> None:
        self._now = self._later + self._now
        self._later = []


def _is_later(frame, later: set) -> bool:
    return (frame is not None and frame.seq in later
            and frame.type in (config.ACK, config.NAK, config.NOTE))


def drain_reply(transport, wait: float, hb, codec: str = config.CODEPAGE,
                later: set | None = None) -> bool:
    """Дочитать ответ, пришедший сразу за подтверждением.

    Порядок ответов таков, что ACK уходит раньше вывода: подтверждение нужно
    даже если исполнение зависло. Выход по первому ACK оставил бы NOTE с выводом
    в очереди транспорта, и тот припечатался бы к следующей команде. Ждём
    idle-паузу после каждого кадра, чтобы собрать пачку NOTE длинного вывода, а
    не только первый.

    True — среди дочитанного был ACK. Вызвавший, который закрыл попытку по
    NOTE, так отличает «ACK пришёл следом» от «в снимке был только ответ».
    """
    saw_ack = False
    reader = transport if isinstance(transport, _Reader) else None
    while True:
        if reader is not None:
            frame, err = reader.receive(wait)
        else:
            frame, err = app.receive_frame(transport, wait)
        if frame is None:
            if err is not None:
                app.warn(f"?? ответ испорчен ({err}, code={err.code:#04x})")
            return saw_ack
        if app.heartbeat_handle(hb, transport if reader is None else reader.transport, frame):
            continue
        _back(hb)
        if reader is not None and _is_later(frame, later or set()):
            reader.park(frame)
            continue
        if frame.type in config.REPLY_TYPES:
            show_reply(frame, codec)
            continue
        # Всё остальное (чужой ACK/NAK, повтор) не наш ответ: проглотить его
        # нельзя, но и слать команду второй раз из-за него нельзя.
        if frame.type == config.ACK:
            saw_ack = True
        app.say(f"<< {frame}")
        return saw_ack


def _label(text: str) -> str:
    """Короткий вид команды в строке журнала. Сам кадр несёт текст целиком."""
    if len(text) <= 80:
        return text
    return text[:77] + "..."


def _pieces(text: str, codec: str, counter):
    """Кадры одной команды: (seq, payload, подпись в журнале).

    Номера берутся один раз, до попыток. Повтор длинной команды обязан
    повторить каждый кусок с тем же seq, иначе приёмник примет его за новую
    команду и исполнит второй раз.
    """
    raw = text.encode(codec, errors="replace")
    if len(raw) <= config.MAX_PAYLOAD:
        return [(counter.take(), raw, f"{text!r}")]
    chunks = segment.split(raw)
    if len(chunks) > 255:
        raise ValueError(
            f"команда {len(raw)} байт — это {len(chunks)} кусков, "
            "в заголовок помещается не больше 255"
        )
    seqs = [counter.take() for _ in chunks]
    shown = _label(text)
    total = len(chunks)
    return [
        (seqs[i], segment.pack(seqs[0], i, total, chunk),
         f"{shown!r} часть {i + 1}/{total}")
        for i, chunk in enumerate(chunks)
    ]


def _confirmed(args, reader: _Reader, hb, codec: str, seq: int,
               reply_wait: float, later: set | None = None) -> bool:
    """Дождаться подтверждения этого seq. False — попытку надо повторить.

    Закрывают попытку ACK с нашим seq и NOTE с нашим seq. Чужой NOTE только
    печатается. Чужой ACK попытку не сжигает: это подтверждение не этой
    команды, и обрыв ожидания из-за него заставлял слать команду заново,
    хотя ответ ещё был в пути.
    """
    timeout = app.wait_timeout(args)
    deadline = time.monotonic() + timeout
    held = later or set()
    reader.release()
    while time.monotonic() < deadline:
        left = max(0.05, deadline - time.monotonic())
        frame, err = reader.receive(left)
        if err is not None:
            app.warn(f"?? подтверждение испорчено ({err}, code={err.code:#04x})")
            return False
        if frame is None:
            app.warn(f".. тишина {timeout:.2f} с, подтверждения нет")
            return False
        # PING собеседника обязан получить PONG даже посреди ожидания ACK:
        # иначе он объявит нас мёртвыми ровно тогда, когда мы ждём ответа.
        if app.heartbeat_handle(hb, reader.transport, frame):
            continue
        _back(hb)
        if _is_later(frame, held):
            reader.park(frame)
            continue
        if frame.type == config.ACK and frame.seq == seq:
            app.say(f"<< ACK seq={frame.seq}")
            drain_reply(reader, reply_wait, hb, codec, held)
            return True
        if frame.type == config.NAK and frame.seq == seq:
            # Причина — байт NAK_* в payload, поэтому показываем payload как
            # есть: угадывать по длине здесь значит молча потерять причину.
            app.warn(f"<< NAK seq={frame.seq} payload={frame.payload.hex()} "
                     f"— не подтверждён, повторю")
            return False
        if frame.type in config.REPLY_TYPES and frame.seq == seq:
            show_reply(frame, codec)
            if not drain_reply(reader, reply_wait, hb, codec, held):
                app.say(f".. ответ seq={frame.seq} пришёл без ACK, попытка закрыта")
            return True
        if frame.type in config.REPLY_TYPES:
            show_reply(frame, codec)
            continue
        if frame.type == config.PONG or frame.type == config.ACK:
            app.say(f"<< {frame.type_name} seq={frame.seq}")
            continue
        app.warn(f"?? ожидался ACK seq={seq}, получен {frame}")
        return False
    return False


def _cost(text: str, codec: str):
    """Сколько кадров и байт займёт команда, не занимая seq.

    Третье значение — команда длиннее кадра. Такая едет своим снимком:
    куски одной команды нельзя отрывать друг от друга, а ответ на них
    занимает ячейки, которые иначе достались бы соседним командам.
    """
    raw = text.encode(codec, errors="replace")
    if len(raw) <= config.MAX_PAYLOAD:
        return 1, 7 + len(raw), False
    chunks = segment.split(raw)
    total = len(chunks)
    nbytes = 0
    for index, chunk in enumerate(chunks):
        payload = segment.pack(0, index, total, chunk)
        nbytes += 7 + len(payload)
    return total, nbytes, True


def groups(texts, codec: str = config.CODEPAGE):
    """Разложить очередь по снимкам.

    Коротких команд в снимке — сколько уже ждёт, но не больше BATCH_COMMANDS,
    HISTORY_SLOTS кадров и SNAPSHOT_MAX байт. Длинная команда — отдельный снимок.
    """
    batches = []
    current = []
    frames = 0
    nbytes = 0
    for text in texts:
        nframes, size, segmented = _cost(text, codec)
        if segmented:
            if current:
                batches.append(current)
                current, frames, nbytes = [], 0, 0
            batches.append([text])
            continue
        overflow = (
            len(current) + 1 > config.BATCH_COMMANDS
            or frames + nframes > config.HISTORY_SLOTS
            or nbytes + size > SNAPSHOT_MAX
        )
        if current and overflow:
            batches.append(current)
            current, frames, nbytes = [], 0, 0
        current.append(text)
        frames += nframes
        nbytes += size
    if current:
        batches.append(current)
    return batches


def _confirmed_many(args, reader: _Reader, hb, codec: str, seqs,
                    reply_wait: float) -> bool:
    """Дождаться подтверждения каждого seq пачки. False — повторить пачку.

    Ответ приходит одной записью: ACK и NOTE каждой команды лежат в очереди
    сразу. Пока набор не закрыт, кадр из очереди не считается тишиной.
    """
    need = set(seqs)
    timeout = app.wait_timeout(args)
    deadline = time.monotonic() + timeout
    reader.release()
    while need and time.monotonic() < deadline:
        left = max(0.05, deadline - time.monotonic())
        frame, err = reader.receive(left)
        if err is not None:
            app.warn(f"?? подтверждение испорчено ({err}, code={err.code:#04x})")
            return False
        if frame is None:
            app.warn(f".. тишина {timeout:.2f} с, подтверждения нет")
            return False
        if app.heartbeat_handle(hb, reader.transport, frame):
            continue
        _back(hb)
        if frame.type == config.ACK and frame.seq in need:
            app.say(f"<< ACK seq={frame.seq}")
            need.discard(frame.seq)
            continue
        if frame.type == config.NAK and frame.seq in need:
            app.warn(f"<< NAK seq={frame.seq} payload={frame.payload.hex()} "
                     f"— не подтверждён, повторю")
            return False
        if frame.type in config.REPLY_TYPES and frame.seq in need:
            show_reply(frame, codec)
            need.discard(frame.seq)
            continue
        if frame.type in config.REPLY_TYPES:
            show_reply(frame, codec)
            continue
        if frame.type == config.PONG or frame.type == config.ACK:
            app.say(f"<< {frame.type_name} seq={frame.seq}")
            continue
        app.warn(f"?? ожидался ACK из {sorted(need)}, получен {frame}")
        return False
    if need:
        app.warn(f".. тишина {timeout:.2f} с, подтверждения нет")
        return False
    drain_reply(reader, reply_wait, hb, codec)
    return True


def _send_prepared(args, transport, prepared, hb, codec: str) -> bool:
    """Отправить уже нарезанные команды одним снимком и дождаться ответа.

    prepared — список команд, каждая команда — список кусков из _pieces.
    Номера кусков уже выданы: повтор пачки несёт те же seq.
    """
    for attempt in range(1, args.retries + 1):
        frames = [framing.build_frame(config.CMD, seq, payload)
                  for pieces in prepared
                  for seq, payload, _label in pieces]
        if hasattr(transport, "_nudge_once"):
            # Первая запись после чужого снимка на редиректе часто пропадает.
            # Вторая, новым номером ячейки, доходит. Ответ так не повторяем:
            # лишняя запись стирает команду, которая уже в пути.
            transport._nudge_once = True
        if len(frames) == 1:
            fail = app.send_frame(transport, frames[0])
        else:
            fail = app.send_frames(transport, frames)
        if fail is not None:
            app.warn(f"?? CMD не отправлен: {fail}")
            continue
        for pieces in prepared:
            for seq, _payload, label in pieces:
                app.say(f">> CMD seq={seq} {label} (попытка {attempt})")
        if args.no_ack:
            return True
        reader = _Reader(transport)
        closing = [pieces[-1][0] for pieces in prepared]
        # Ждём только последний кусок каждой команды. Промежуточный ACK
        # приёмник затирает ответом на последний.
        if len(closing) == 1:
            ok = _confirmed(args, reader, hb, codec, closing[0], args.reply_wait, set())
        else:
            ok = _confirmed_many(args, reader, hb, codec, closing, args.reply_wait)
        if ok:
            return True
        tune.on_exchange_failed()
    return False


def send_batch(args, transport, counter, texts, hb, codec: str = config.CODEPAGE) -> bool:
    """Отправить очередь. Короткие команды делят снимок, длинная едет одна.

    False — хотя бы один снимок не подтверждён.
    """
    ok = True
    tune.touch()
    for group in groups(texts, codec):
        prepared = []
        try:
            for text in group:
                prepared.append(_pieces(text, codec, counter))
        except ValueError as exc:
            app.warn(f"?? команда не отправлена: {exc}")
            return False
        if not _send_prepared(args, transport, prepared, hb, codec):
            ok = False
    return ok


def send_one(args, transport, counter, text: str, hb, codec: str = config.CODEPAGE) -> bool:
    """Один обмен: CMD, затем подтверждение с тем же seq. True — подтверждено.

    Текст длиннее кадра уходит несколькими CMD одной записью буфера. Вторая
    запись сразу за первой на живом редиректе не доезжает: первый кусок
    подтверждался, второй пропадал. Попытка — это вся команда. Сорвался один
    кусок — со следующей попытки уходят все куски заново, с теми же номерами.
    """
    return send_batch(args, transport, counter, [text], hb, codec)


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
        if tune.active() is not None:
            tune.active().seen()
        _back(hb)
        app.say(f"<< {frame}")
    if tune.active() is not None:
        tune.active().maybe_idle(transport)
    app.heartbeat_tick(hb, transport)
    app.heartbeat_check(hb, transport)


def _ready_lines(lines, first: str):
    """Забрать всё, что уже лежит во вводе, не дожидаясь следующей строки.

    Пока человек думает, очередь пустая и снимку нечего уплотнять. Несколько
    команд, введённых подряд, уходят одной записью.
    """
    stop = False
    batch = []

    def take(line) -> bool:
        nonlocal stop
        if line is None or line.strip() in (".q", ".quit", ".exit"):
            stop = True
            return False
        if line.strip():
            batch.append(line)
        return True

    if not take(first):
        return batch, stop
    while True:
        try:
            nxt = lines.get_nowait()
        except Empty:
            break
        if not take(nxt):
            break
    return batch, stop


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
        if tune.windows(transport):
            tune.use(tune.Pace())
            if tune.active().measure(transport, "старт"):
                hb.note()
        if args.command:
            if not send_batch(args, transport, counter, list(args.command), hb, codec):
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
                batch, stop = _ready_lines(lines, line)
                if batch and not send_batch(args, transport, counter, batch, hb, codec):
                    failures += 1
                if stop:
                    break
    finally:
        channel.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())