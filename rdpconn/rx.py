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

Своих контрольных PING rx не заводит (см. app.Heartbeat.proactive): отправитель
и так пингует, а каждый PONG — уже признак жизни. Замер паузы повтора rx делает
отдельно и только когда команд давно не было: иначе две проверки писали бы в
слот одновременно.

Остановка: --limit по числу кадров, --duration по времени. Без обоих слушает
вечно до Ctrl-C.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from . import app, config, framing, segment, tune


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
    piece = segment.unpack(payload) if frame.type == config.CMD else None
    if piece is not None:
        _group, index, count, _data = piece
        return f"{frame} часть {index + 1}/{count}"
    if payload and len(payload) <= 48:
        if frame.type in (config.CMD, config.NOTE):
            return f"{frame} payload={payload.decode(codec, errors='replace')!r}"
        return f"{frame} payload={payload.hex()}"
    return str(frame)


class ReplyCache:
    """Ответ на CMD, уже собранный для этого seq.

    Повтор протокола обязан нести тот же seq (docs/protocol.md 4). Исполнять
    команду второй раз нельзя: повтор — это «доставь прежний ответ», а не
    новая работа. Ключ — пара (seq, payload). Один только seq после оборота
    счётчика совпал бы с новой командой, и кэш отдал бы чужой вывод.
    """

    def __init__(self) -> None:
        self._items = {}

    def get(self, seq: int, payload: bytes):
        item = self._items.get(seq)
        if item is None or item[0] != payload:
            return None
        return item[1], item[2]

    def put(self, seq: int, payload: bytes, frames: list, echo: bool) -> None:
        self._items[seq] = (bytes(payload), list(frames), echo)


def _run_command(text: str, timeout: float):
    """Запустить строку команды и вернуть CompletedProcess.

    shell=True на Windows собирает `cmd /c "<текст>"`. У cmd при этом своё
    правило кавычек: последняя кавычка остаётся на последнем слове, и
    `cmd /c set` превращается в программу `set"`. Поэтому на Windows строка
    уходит в `cmd /d /s /c` без второй оболочки: /s снимает кавычки
    предсказуемо, /d не читает AutoRun.
    """
    if os.name == "nt":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return subprocess.run(
            [comspec, "/d", "/s", "/c", text],
            capture_output=True,
            timeout=timeout,
        )
    return subprocess.run(
        text, capture_output=True, shell=True, timeout=timeout,
    )


def _exec_output(text: str, timeout: float, codec: str) -> str:
    """Вывод команды. Ошибка тоже текст: пустой ответ неотличим от молчания."""
    try:
        # Без text=True: он декодирует вывод локальной ANSI-страницей (cp1251),
        # а cmd.exe пишет в OEM-странице консоли. Кириллица в выводе
        # превращалась бы в '?' ещё до попадания в кадр.
        proc = _run_command(text, timeout)
        raw = proc.stdout if proc.returncode == 0 else proc.stderr
        return raw.decode(codec, errors="replace")[:config.EXEC_OUTPUT_LIMIT]
    except subprocess.TimeoutExpired:
        return f"команда прервана по таймауту {timeout:.0f} с"
    except Exception as exc:
        return f"ошибка: {exc}"


# Команда короче этого отвечает сразу, и повтор ответа стёр бы следующую.
# Дольше — ответ пишется после долгого молчания и с первой записи не уезжает,
# пока отправитель ещё ждёт. Один повтор новой ячейкой, как у команды.
SLOW_REPLY_S = 8.0


def arm_slow_reply(transport, elapsed: float) -> None:
    """Один повтор ответа, если команда исполнялась долго."""
    if elapsed < SLOW_REPLY_S:
        return
    if hasattr(transport, "_nudge_once"):
        transport._nudge_once = True


def _reply_for(args, seq: int, payload: bytes, codec: str):
    """ACK и следующие за ним NOTE для уже собранной команды.

    Команда здесь исполняется один раз. Пустое эхо тоже уходит одним NOTE:
    молчание на пустую команду неотличимо от потери кадра.
    Третье значение — сколько секунд заняло исполнение. У эха это ноль.
    """
    frames = [framing.ack(seq)]
    if args.exec_:
        started = time.monotonic()
        output = _exec_output(
            payload.decode(codec, errors="replace"),
            args.exec_timeout, codec,
        )
        elapsed = time.monotonic() - started
        # Ограничение payload = 240 байт, поэтому разбиваем на NOTE-фразы.
        for i in range(0, len(output), config.MAX_PAYLOAD):
            chunk = output[i:i + config.MAX_PAYLOAD].encode(codec, errors="replace")
            frames.append(framing.build_frame(config.NOTE, seq, chunk))
        return frames, False, elapsed
    if not args.no_echo:
        if not payload:
            frames.append(framing.echo(seq, payload))
        else:
            for i in range(0, len(payload), config.MAX_PAYLOAD):
                frames.append(framing.build_frame(
                    config.NOTE, seq, payload[i:i + config.MAX_PAYLOAD]))
        return frames, True, 0.0
    return frames, False, 0.0


def _reply_frames(args, frame, codec: str):
    """ACK и NOTE на один кадр, который сам и есть команда."""
    frames, echo, _elapsed = _reply_for(args, frame.seq, frame.payload, codec)
    return frames, echo


def _log_sent(frames, echo: bool) -> None:
    for raw in frames:
        frame = framing.parse_frame(raw)
        if frame.type == config.ACK:
            app.say(f">> ACK seq={frame.seq}")
        elif echo:
            app.say(f">> NOTE seq={frame.seq} (эхо)")
        else:
            app.say(f">> NOTE seq={frame.seq} len={len(frame.payload)}")


def _emit_reply(transport, frames, echo: bool) -> None:
    """Отправить уже собранный ответ: и первый раз, и повтор из кэша.

    Пачка уходит одной записью буфера. По кадру редирект успевает увезти
    промежуточный снимок, и хвост вывода до отправителя не доезжает.
    """
    fail = app.send_frames(transport, frames)
    if fail is None:
        _log_sent(frames, echo)
        return
    frame = framing.parse_frame(frames[0])
    if frame.type == config.ACK:
        app.warn(f"?? ACK seq={frame.seq} не отправлен: {fail}")
    else:
        app.warn(f"?? NOTE seq={frame.seq} не отправлен: {fail}")


def _queued_cmds(transport):
    """CMD, которые уже лежат в очереди транспорта.

    Куски одной команды приходят одной записью слота. Пока мы не ответили
    на первый, остальные уже разобраны. Отвечать на первый отдельно — значит
    занять редирект снимком, после которого настоящий ответ уже не доезжает.
    """
    peek = getattr(transport, "peek_pending", None)
    pop = getattr(transport, "pop_pending", None)
    if peek is None or pop is None:
        return
    while True:
        raw = peek()
        if not raw:
            return
        try:
            nxt = framing.parse_frame(raw)
        except framing.FrameError:
            return
        if nxt.type != config.CMD:
            return
        pop()
        yield nxt


def _plain_waiting(transport) -> bool:
    """В очереди этого снимка есть ещё одна короткая команда."""
    peek = getattr(transport, "peek_pending", None)
    if peek is None:
        return False
    raw = peek()
    if not raw:
        return False
    try:
        nxt = framing.parse_frame(raw)
    except framing.FrameError:
        return False
    return nxt.type == config.CMD and segment.unpack(nxt.payload) is None


def _pull_one_plain(transport):
    """Следующая короткая команда, уже лежащая в очереди этого снимка.

    Кусок сегмента не забираем: он принадлежит длинной команде и собирается
    своим путём. Чужой тип тоже остаётся в очереди.
    """
    peek = getattr(transport, "peek_pending", None)
    pop = getattr(transport, "pop_pending", None)
    if peek is None or pop is None:
        return None
    raw = peek()
    if not raw:
        return None
    try:
        nxt = framing.parse_frame(raw)
    except framing.FrameError:
        return None
    if nxt.type != config.CMD or segment.unpack(nxt.payload) is not None:
        return None
    pop()
    return nxt


def _take_reply(args, fr, codec, cache):
    """(кадры, эхо, секунды исполнения) для одной команды, из кэша или заново."""
    cached = cache.get(fr.seq, fr.payload) if cache is not None else None
    if cached is None:
        part, part_echo, elapsed = _reply_for(args, fr.seq, fr.payload, codec)
        if cache is not None:
            cache.put(fr.seq, fr.payload, part, part_echo)
        return part, part_echo, elapsed
    part, part_echo = cached
    return part, part_echo, 0.0


def _reply_coalesced(args, transport, first, codec, cache) -> None:
    """Один снимок ответа на короткие команды, уже лежащие в этом приёме.

    Следующую команду берём, только если в снимке ещё есть место хотя бы
    под её ACK и один NOTE. ACK каждой обязателен: по нему отправитель
    видит, какой seq дошёл. NOTE, которым не хватило ячеек, в этот снимок
    не пишутся — иначе запись затёрла бы собственные подтверждения.
    """
    frames_out = []
    echo = False
    elapsed_max = 0.0
    commands = [first]
    while len(commands) <= config.BATCH_COMMANDS:
        fr = commands[-1]
        if fr is not first:
            app.say(f"<< {describe(fr, codec)}")
            if args.show_hex:
                app.say(f"   hex={fr.payload.hex()}")
        part, part_echo, elapsed = _take_reply(args, fr, codec, cache)
        echo = echo or part_echo
        elapsed_max = max(elapsed_max, elapsed)
        frames_out.extend(part)
        if len(commands) >= config.BATCH_COMMANDS:
            break
        if len(frames_out) + 2 > config.HISTORY_SLOTS:
            break
        nxt = _pull_one_plain(transport)
        if nxt is None:
            break
        commands.append(nxt)
    if len(frames_out) > config.HISTORY_SLOTS:
        acks = []
        notes = []
        for raw in frames_out:
            if framing.parse_frame(raw).type == config.ACK:
                acks.append(raw)
            else:
                notes.append(raw)
        room = max(0, config.HISTORY_SLOTS - len(acks))
        frames_out = acks + notes[:room]
        app.warn(".. часть вывода не влезла в снимок")
    arm_slow_reply(transport, elapsed_max)
    _emit_reply(transport, frames_out, echo)


def handle(args, transport, frame, err, codec: str = config.CODEPAGE,
           cache: ReplyCache | None = None,
           assembly: segment.Assembly | None = None) -> bool:
    """Обработать один приём. True — засчитать событие (для --limit)."""
    if frame is not None:
        app.say(f"<< {describe(frame, codec)}")
        if args.show_hex:
            app.say(f"   hex={frame.payload.hex()}")
        # Подтверждаем только CMD: это то, что реально шлёт наша пара (tx).
        # ACK на ACK или ACK на чужой тип развёл бы бесконечный обмен, а
        # протокол требует ответа лишь кадрам, ждущим подтверждения.
        if frame.type == config.CMD:
            tune.touch()
        if not args.no_reply and frame.type == config.CMD:
            # Короткие команды одного снимка отвечаются одной записью,
            # в том числе вывод --exec, пока он помещается в ячейки.
            if (segment.unpack(frame.payload) is None
                    and _plain_waiting(transport)):
                _reply_coalesced(args, transport, frame, codec, cache)
                return True
            cached = cache.get(frame.seq, frame.payload) if cache is not None else None
            if cached is None:
                kind, body = ("plain", frame.payload)
                if assembly is not None:
                    kind, body = assembly.take(frame.payload)
                partials = []
                answered = frame
                if kind == "wait" and assembly is not None:
                    # Следующие куски уже в очереди — собираем их до записи
                    # в буфер. Промежуточный ACK отдельной записью оставлял
                    # на хосте себя, а ответ последнего куска не доезжал.
                    partials.append(frame)
                    for nxt in _queued_cmds(transport):
                        app.say(f"<< {describe(nxt, codec)}")
                        if args.show_hex:
                            app.say(f"   hex={nxt.payload.hex()}")
                        nkind, nbody = assembly.take(nxt.payload)
                        if nkind == "wait":
                            partials.append(nxt)
                            continue
                        kind, body, answered = nkind, nbody, nxt
                        break
                if kind == "wait":
                    frames = [framing.ack(part.seq) for part in (partials or [frame])]
                    echo = False
                    if cache is not None:
                        for part, raw_ack in zip(partials or [frame], frames):
                            cache.put(part.seq, part.payload, [raw_ack], False)
                elif kind == "ready":
                    frames, echo, elapsed = _reply_for(args, answered.seq, body, codec)
                    arm_slow_reply(transport, elapsed)
                    if cache is not None:
                        for part in partials:
                            cache.put(part.seq, part.payload, [framing.ack(part.seq)], False)
                        cache.put(answered.seq, answered.payload, frames, echo)
                else:
                    frames, echo = _reply_frames(args, answered, codec)
                    if cache is not None:
                        cache.put(answered.seq, answered.payload, frames, echo)
            else:
                frames, echo = cached
            _emit_reply(transport, frames, echo)
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


def step(args, transport, hb, frame, err, codec: str = config.CODEPAGE,
         cache: ReplyCache | None = None,
         assembly: segment.Assembly | None = None) -> bool:
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
    return handle(args, transport, frame, err, codec, cache, assembly)


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
    if tune.windows(transport):
        # Замер здесь позже, чем у отправителя, и только на простое без команд.
        tune.use(tune.Pace(idle_offset=90.0))
    cache = ReplyCache()
    assembly = segment.Assembly()
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
            if frame is not None and tune.active() is not None:
                tune.active().seen()
            elif tune.active() is not None and err is None:
                tune.active().maybe_idle(transport)
            if step(args, transport, hb, frame, err, codec, cache, assembly):
                received += 1
    except KeyboardInterrupt:
        app.say("")
    finally:
        channel.close()
    app.say(f"принято событий: {received}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
