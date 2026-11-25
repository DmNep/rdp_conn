"""Выход в интернет для программы по ту сторону RDP.

На машине без сети слушает локальный HTTP-прокси (`listen`). Кода или
VS Code смотрит в него через requestOptions.proxy. Байты запроса едут
кадрами NOTE по тому же буферу, формат кадра не меняется. На машине с
интернетом (`dial`) эти байты выходят в обычный TCP и ответ возвращается
тем же снимком.

Одновременно с tx/rx шлюз не запускают: слот один.
"""

from __future__ import annotations

import argparse
import errno
import select
import socket
import sys
import time
from urllib.parse import urlsplit

from . import app, config, framing, monitor

MAGIC = b"G1"
OP_OPEN = 1
OP_OK = 2
OP_FAIL = 3
OP_DATA = 4
OP_END = 5
OP_ACK = 6
OP_POLL = 7
# Пауза клавишей на одной стороне. Без этих кадров вторая сторона
# переиздаёт снимок через секунду и слот снова занят.
OP_HALT = 8
OP_GO = 9
# Короткий ход: в снимке только подтверждения. Занятая сторона пишет следом ещё раз.
# Больше одного пропуска подряд не бывает, иначе обратный запрос застревает.
OP_YIELD = 10
_HEAD = 9  # magic + op + id + offset
DATA_MAX = config.MAX_PAYLOAD - _HEAD
MAX_STREAMS = 2
# Рукопожатие TLS через буфер занимает около 8 с. Пачка параллельных
# соединений не успевает ни одно, слоты остаются заняты, и проверка
# «Кода онлайн» больше не стартует.
_STALL_S = 20.0
# Оба направления уже сходили: это живой туннель, а не оборванное рукопожатие.
# Его можно держать тихим, чтобы следующий запрос Коды не начинал TLS заново.
_WARM_S = 180.0
# Мелкие куски TCP подождать и увезти одним снимком. Полный снимок не ждёт.
_COALESCE_S = 0.15
# Ячеек в одном снимке шлюза. 20 ячеек протокола команд сюда не входят:
# у шлюза свой потолок, пока текст слота ниже размера, который редирект увозит.
# Пишем с 40 ячеек. Читаем сразу до 58, иначе чужая ступень выглядит как потеря.
# 40 ячеек — около 14 тысяч символов. 58 — около 20 тысяч, ниже записи в 16 КБ.
GATE_CELLS_MIN = 40
GATE_CELLS_STEP = 6
GATE_CELLS_MAX = 58
GATE_CELLS = GATE_CELLS_MAX
GATE_TEXT_MIN = 14000
GATE_TEXT_MAX = GATE_TEXT_MIN * GATE_CELLS_MAX // GATE_CELLS_MIN
_WINDOW = 64 * 1024
_CONNECT_WAIT = 4.0
# Неблокирующий connect: эти коды значат «ещё идёт», а не ошибку.
_PENDING = {0, 10035, 10036}
for _name in ("EINPROGRESS", "EWOULDBLOCK", "EALREADY"):
    _code = getattr(errno, _name, None)
    if _code is not None:
        _PENDING.add(_code)


class Pipe:
    """Один TCP с обеих сторон слота. Смещение в DATA — чтобы повтор не двоился."""

    def __init__(self, sid: int) -> None:
        self.sid = sid
        self.sock: socket.socket | None = None
        self.to_peer = bytearray()
        self.base = 0
        self.local_got = 0
        self.to_sock = bytearray()
        self.target: tuple[str, int] | None = None
        self.mode = ""
        self.opened = False
        self.failed = ""
        self.local_eof = False
        self.remote_eof = False
        self.end_sent = False
        self.greeted = False
        self.early = b""
        self.connecting = False
        self.connect_at = 0.0
        self.told = False
        self.saw_peer = False
        self.offered = 0
        self.touch = time.monotonic()
        self.read_n = 0
        self.wrote_n = 0


def pack(op: int, sid: int, body: bytes = b"", offset: int = 0) -> bytes:
    payload = MAGIC + bytes((op,)) + sid.to_bytes(2, "big")
    if op in (OP_DATA, OP_ACK):
        payload += offset.to_bytes(4, "big") + body
    else:
        payload += body
    if len(payload) > config.MAX_PAYLOAD:
        raise ValueError(f"шлюз {len(payload)} байт больше кадра")
    return payload


def unpack(payload: bytes):
    if len(payload) < 5 or payload[:2] != MAGIC:
        return None
    op = payload[2]
    sid = int.from_bytes(payload[3:5], "big")
    if op in (OP_DATA, OP_ACK):
        if len(payload) < _HEAD:
            return None
        offset = int.from_bytes(payload[5:9], "big")
        return op, sid, offset, payload[_HEAD:]
    return op, sid, 0, payload[5:]


def _meter_in(sid: int, n: int) -> None:
    """Байты, прочитанные из своего сокета. На хосте это ответ сайта."""
    face = monitor.current()
    if face is None or n <= 0:
        return
    if face.mode == "dial":
        face.add(sid, down=n)
    else:
        face.add(sid, up=n)


def _meter_out(sid: int, n: int) -> None:
    """Байты, записанные в свой сокет. На хосте это запрос к сайту."""
    face = monitor.current()
    if face is None or n <= 0:
        return
    if face.mode == "dial":
        face.add(sid, up=n)
    else:
        face.add(sid, down=n)


def _meter_open(sid: int, target: str) -> None:
    face = monitor.current()
    if face is not None:
        face.open(sid, target)


def _meter_ready(sid: int) -> None:
    face = monitor.current()
    if face is not None:
        face.ready(sid)


def _meter_close(sid: int, why: str) -> None:
    face = monitor.current()
    if face is not None:
        face.close(sid, why)


def _meter_hold(transport) -> None:
    face = monitor.current()
    if face is None:
        return
    if getattr(transport, "yielding", False):
        face.hold_text("буфер ваш")
    elif getattr(transport, "paused", False):
        face.hold_text("пауза")
    else:
        face.hold_text("")


def _frame(seq: app.SeqCounter, op: int, sid: int, body: bytes = b"", offset: int = 0) -> bytes:
    return framing.build_frame(config.NOTE, seq.take(), pack(op, sid, body, offset))


def _cell_chars(raw: bytes) -> int:
    """Сколько символов ячейка займёт в тексте слота, с запасом на номер."""
    return 12 + 4 * ((len(raw) + 2) // 3)


_BIAS_WINDOW_S = 3.0
# Чужое направление должно быть заметно толще, иначе каждый круг будет прыгать.
_BIAS_MIN_B = 1024


class Bias:
    """Кому отдать следующий полный снимок.

    Очередь прямо сейчас важнее среднего. Среднее за 3 с только не даёт
    переключаться, пока перевес не подержался. Рукопожатие сюда не входит.
    """

    def __init__(self) -> None:
        self.peer_data = 0
        self.yielded = False
        self.told = False
        self.sent = 0
        self.recv = 0
        self._gave = False
        self._took = False
        self._samples: list[tuple[float, int, int]] = []

    def note_peer(self, frames) -> None:
        data_n = _data_bytes(frames)
        self.recv += data_n
        if data_n:
            self.peer_data += 1
        if self._gave:
            self._credit("gave", data_n)
            self._gave = False
        if _has_yield(frames):
            self._took = True
        self._mark()

    def note_sent(self, frames, yielded: bool) -> None:
        data_n = _data_bytes(frames)
        self.sent += data_n
        if self._took and not yielded:
            self._credit("took", data_n)
            self._took = False
        if yielded:
            self.yielded = True
            self._gave = True
        else:
            self.yielded = False
            self.peer_data = 0
        self._mark()
        if yielded and not self.told:
            app.say(".. чужое направление больше, короткий ход")
            self.told = True
        if not yielded and self.told and not self.peer_heavier():
            app.say(".. ход снова поровну")
            self.told = False

    def _credit(self, kind: str, n: int) -> None:
        face = monitor.current()
        if face is not None:
            face.turn(kind, n)

    def should_yield(self, pipes: dict[int, Pipe]) -> bool:
        if self.yielded or self.peer_data < 1:
            return False
        if _bias_blocked(pipes) or _backlog(pipes) > 0:
            return False
        return self.peer_heavier()

    def peer_heavier(self) -> bool:
        if not self._samples:
            return False
        start = self._samples[0]
        span = time.monotonic() - start[0]
        if span < 2.0:
            return False
        sent = self.sent - start[1]
        recv = self.recv - start[2]
        return recv >= sent + _BIAS_MIN_B and recv > sent * 2

    def _mark(self) -> None:
        now = time.monotonic()
        self._samples.append((now, self.sent, self.recv))
        cutoff = now - _BIAS_WINDOW_S
        keep = 0
        for index, sample in enumerate(self._samples):
            if sample[0] >= cutoff:
                keep = max(0, index - 1)
                break
        if keep:
            del self._samples[:keep]


def _is_yield(raw: bytes) -> bool:
    try:
        item = unpack(framing.parse_frame(raw).payload)
    except framing.FrameError:
        return False
    return item is not None and item[0] == OP_YIELD


def _note_payload(frame):
    if isinstance(frame, (bytes, bytearray)):
        try:
            frame = framing.parse_frame(frame)
        except framing.FrameError:
            return None
    if getattr(frame, "type", None) != config.NOTE:
        return None
    return frame.payload


def _has_yield(frames) -> bool:
    for frame in frames:
        payload = _note_payload(frame)
        if payload is None:
            continue
        item = unpack(payload)
        if item is not None and item[0] == OP_YIELD:
            return True
    return False


def _data_bytes(frames) -> int:
    total = 0
    for frame in frames:
        payload = _note_payload(frame)
        if payload is None:
            continue
        item = unpack(payload)
        if item is None:
            continue
        op, _sid, _offset, body = item
        if op == OP_DATA and body:
            total += len(body)
    return total


def _backlog(pipes: dict[int, Pipe]) -> int:
    return sum(max(0, pipe.base + len(pipe.to_peer) - pipe.offered) for pipe in pipes.values())


def _bias_blocked(pipes: dict[int, Pipe]) -> bool:
    """Рукопожатие и ещё не открытый поток ход не отдают."""
    for pipe in pipes.values():
        if pipe.failed or (pipe.local_eof and pipe.remote_eof):
            continue
        if pipe.connecting or (pipe.sock is not None and not pipe.opened):
            return True
        if pipe.mode and not pipe.opened:
            return True
        if pipe.opened and pipe.sock is not None and (pipe.read_n == 0 or pipe.wrote_n == 0):
            return True
    return False


def text_limit(cells: int) -> int:
    """Символы слота растут вместе с ячейками, иначе новая ступень пустая."""
    return GATE_TEXT_MIN * cells // GATE_CELLS_MIN


class Ceiling:
    """Сколько ячеек класть в свой снимок.

    Вверх — только полный снимок, который подтвердили. Обычный повтор
    через 0.94 с потерей не считается. Одна потеря минус 6, вторая подряд
    без чистого полного снимка — сразу на 40 и до следующей паузы.
    """

    def __init__(self) -> None:
        self.cells = GATE_CELLS_MIN
        self.allow = True
        self.strikes = 0
        self.armed = False
        self.nudge_seen = False
        self.need: dict[int, int] = {}
        self.quiet_since: float | None = None

    @property
    def text_max(self) -> int:
        return text_limit(self.cells)

    def arm(self, pipes: dict[int, Pipe], transport) -> None:
        self.observe(pipes)
        self.need = {
            pipe.sid: pipe.offered
            for pipe in pipes.values()
            if pipe.offered > pipe.base
        }
        self.armed = bool(self.need)
        self.nudge_seen = False
        if self.armed:
            transport.on_nudge = self._mark_nudge

    def clear(self) -> None:
        self.armed = False
        self.need = {}
        self.nudge_seen = False

    def observe(self, pipes: dict[int, Pipe]) -> None:
        self._rearm_idle(pipes)
        if not self.armed:
            return
        if self._confirmed(pipes):
            self._raise()
            return
        if self.nudge_seen:
            self._drop()

    def _mark_nudge(self) -> None:
        self.nudge_seen = True

    def _confirmed(self, pipes: dict[int, Pipe]) -> bool:
        for sid, offset in self.need.items():
            pipe = pipes.get(sid)
            if pipe is not None and pipe.base < offset:
                return False
        return True

    def _raise(self) -> None:
        old = self.cells
        self.strikes = 0
        self.clear()
        if not self.allow or old >= GATE_CELLS_MAX:
            return
        self.cells = min(GATE_CELLS_MAX, old + GATE_CELLS_STEP)
        app.say(f".. потолок {old} → {self.cells}, полный снимок подтверждён")

    def _drop(self) -> None:
        old = self.cells
        self.clear()
        if self.strikes >= 1:
            self.cells = GATE_CELLS_MIN
            self.allow = False
            self.quiet_since = None
            self.strikes = 0
            app.say(f".. потолок {old} → {self.cells}, вторая потеря, жду новую передачу")
            return
        self.strikes = 1
        self.cells = max(GATE_CELLS_MIN, old - GATE_CELLS_STEP)
        if self.cells == GATE_CELLS_MIN:
            self.allow = False
            self.quiet_since = None
        app.say(f".. потолок {old} → {self.cells}, повтор не доехал")

    def _rearm_idle(self, pipes: dict[int, Pipe]) -> None:
        if self.allow:
            return
        busy = self.armed or _backlog(pipes) > 0
        now = time.monotonic()
        if busy:
            self.quiet_since = None
            return
        if self.quiet_since is None:
            self.quiet_since = now
        elif now - self.quiet_since >= 2.0:
            self.allow = True
            self.quiet_since = None
            app.say(".. потолок снова можно поднимать")


def build_frames(pipes: dict[int, Pipe], seq: app.SeqCounter, drive: bool, reserve: int = 0,
                 yield_turn: bool = False, cells: int | None = None,
                 text_max: int | None = None) -> list[bytes]:
    """Снимок: сначала байты живого запроса, в хвосте подтверждения.

    reserve — ячейки под OK/FAIL, иначе повтор старого хвоста вытесняет
    ответ «соединение открыто» и новый запрос Коды молчит до обрыва.
    Потолок записи — текущая ступень, не HISTORY_SLOTS команд.
    """
    cells = GATE_CELLS_MIN if cells is None else cells
    text_max = text_limit(cells) if text_max is None else text_max
    frames: list[bytes] = []
    ack_n = sum(1 for pipe in pipes.values() if pipe.local_got > 0)
    held = min(ack_n, cells - 1) + max(0, reserve)
    if held >= cells:
        held = cells - 1
    room = cells - held
    used = 0

    def add(raw: bytes) -> bool:
        nonlocal room, used
        cost = _cell_chars(raw)
        if room <= 0 or used + cost > text_max:
            return False
        frames.append(raw)
        room -= 1
        used += cost
        return True

    for pipe in pipes.values():
        if pipe.mode and pipe.target is not None and not pipe.opened and not pipe.failed:
            port, host = pipe.target[1], pipe.target[0]
            body = port.to_bytes(2, "big") + host.encode("utf-8", errors="replace")
            if not add(_frame(seq, OP_OPEN, pipe.sid, body)):
                break
    ordered = sorted(pipes.values(), key=lambda pipe: (pipe.sock is None, pipe.sid))
    if not yield_turn:
        for pipe in ordered:
            view = memoryview(pipe.to_peer)
            pos = 0
            if room > 0:
                while pos < len(view) and room > 0:
                    chunk = bytes(view[pos:pos + DATA_MAX])
                    add(_frame(seq, OP_DATA, pipe.sid, chunk, pipe.base + pos))
                    pos += len(chunk)
                pipe.offered = pipe.base + pos
            if room <= 0:
                break
    for pipe in pipes.values():
        if pipe.local_eof and not pipe.to_peer and not pipe.end_sent:
            if add(_frame(seq, OP_END, pipe.sid)):
                pipe.end_sent = True
    room = cells - len(frames)
    for pipe in pipes.values():
        if pipe.local_got <= 0:
            continue
        if not add(_frame(seq, OP_ACK, pipe.sid, offset=pipe.local_got)):
            break
    if yield_turn:
        add(_frame(seq, OP_YIELD, 0))
    if drive and not frames:
        add(_frame(seq, OP_POLL, 0))
    return frames


def _arm_pause(transport) -> None:
    """Клавиша на этой стороне останавливает запись и там, и у собеседника."""
    seq = app.SeqCounter(1)

    def emit(op: int) -> None:
        # Повтор снимка через секунду снова занял бы слот, который только что отдали.
        raw = _frame(seq, op, 0)
        transport._pin_signal = raw
        transport._pin_ready = False
        transport._nudge_once = False
        fail = app.send_frames(transport, [raw])
        transport._nudge_at = None
        transport._nudge_mine = None
        transport._pin_seen = {
            who: set(pairs) for who, pairs in transport._seen_frames.items()
        }
        transport._pin_ready = True
        if fail:
            app.warn(f"?? сигнал паузы не ушёл: {fail}")

    transport.before_pause = lambda: emit(OP_HALT)
    transport.after_resume = lambda: emit(OP_GO)
    _arm_human(transport)


def _arm_human(transport) -> None:
    """Текст человека в слоте останавливает запись, не затирая сам текст.

    Вторая сторона видит тот же слот через редирект и тоже молчит.
    Служебный кадр здесь не шлётся: он стёр бы копию.
    """
    transport.yield_human = True
    store = getattr(transport, "_store", None)
    if store is None or store.__class__.__name__ != "WindowsClipboard":
        return
    if getattr(transport, "_paste_watch", None) is not None:
        return
    from .clipboard import ClipboardError, PasteWatch
    try:
        watch = PasteWatch(transport.note_paste)
        watch.start()
    except ClipboardError as exc:
        app.warn(f"!! вставку не отслеживаю: {exc}")
        return
    transport._paste_watch = watch


def _human_quiet(transport) -> bool:
    consider = getattr(transport, "consider_slot", None)
    if consider is not None:
        consider()
    return bool(getattr(transport, "paused", False) or getattr(transport, "yielding", False))


def apply_frames(pipes: dict[int, Pipe], frames, dial: bool, transport=None) -> None:
    for frame in frames:
        if frame.type != config.NOTE:
            continue
        item = unpack(frame.payload)
        if item is None:
            continue
        op, sid, offset, body = item
        if op == OP_HALT:
            if transport is not None:
                transport.pause(announce=False)
            continue
        if op == OP_GO:
            if transport is not None:
                transport.resume(announce=False)
            continue
        if op == OP_POLL or op == OP_YIELD:
            continue
        pipe = pipes.get(sid)
        if op == OP_OPEN and dial:
            if pipe is not None and pipe.sock is not None and not (pipe.local_eof and pipe.remote_eof):
                continue
            if pipe is not None:
                _abandon(pipe)
                pipes.pop(sid, None)
            if len(body) < 2:
                continue
            port = int.from_bytes(body[:2], "big")
            host = body[2:].decode("utf-8", errors="replace")
            pipe = Pipe(sid)
            pipes[sid] = pipe
            pipe.target = (host, port)
            _begin_connect(pipe)
            continue
        if pipe is None:
            continue
        if op == OP_OK:
            pipe.opened = True
            pipe.touch = time.monotonic()
        elif op == OP_FAIL:
            pipe.failed = body.decode("utf-8", errors="replace") or "отказ"
        elif op == OP_ACK:
            if offset > pipe.base:
                drop = min(len(pipe.to_peer), offset - pipe.base)
                del pipe.to_peer[:drop]
                pipe.base = offset
                pipe.touch = time.monotonic()
        elif op == OP_DATA:
            pipe.saw_peer = True
            pipe.touch = time.monotonic()
            if offset == pipe.local_got and body:
                pipe.to_sock.extend(body)
                pipe.local_got += len(body)
            elif offset > pipe.local_got:
                # Дыра: этот кусок придёт снова, когда догоним подтверждение.
                pass
        elif op == OP_END:
            pipe.saw_peer = True
            pipe.remote_eof = True


def _pull(pipe: Pipe) -> None:
    if pipe.sock is None or pipe.local_eof or pipe.failed:
        return
    if len(pipe.to_peer) >= _WINDOW:
        return
    try:
        chunk = pipe.sock.recv(8192)
    except BlockingIOError:
        return
    except OSError:
        chunk = b""
    if chunk == b"":
        pipe.local_eof = True
        return
    pipe.touch = time.monotonic()
    pipe.read_n += len(chunk)
    pipe.to_peer.extend(chunk)
    _meter_in(pipe.sid, len(chunk))


def _flush(pipe: Pipe) -> None:
    if pipe.sock is None or not pipe.to_sock:
        return
    try:
        sent = pipe.sock.send(pipe.to_sock)
    except BlockingIOError:
        return
    except OSError:
        # Сокет ещё соединяется: на Windows это WSAENOTCONN, не обрыв.
        # Запрос Коды остаётся в to_sock и уйдёт, когда connect завершится.
        if not pipe.opened:
            return
        pipe.local_eof = True
        pipe.to_sock.clear()
        return
    if sent:
        pipe.touch = time.monotonic()
        pipe.wrote_n += sent
        _meter_out(pipe.sid, sent)
    del pipe.to_sock[:sent]


def _close_done(pipes: dict[int, Pipe]) -> None:
    for sid, pipe in list(pipes.items()):
        if pipe.failed and pipe.sock is not None and pipe.mode:
            if pipe.greeted:
                try:
                    pipe.sock.close()
                except OSError:
                    pass
                pipe.sock = None
            else:
                _reject(pipe)
        if pipe.remote_eof and pipe.sock is not None and not pipe.to_sock:
            # Половина закрытия: клиент дописал своё, ответ сервера ещё читаем.
            try:
                pipe.sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        if (pipe.sock is not None and pipe.local_eof and pipe.remote_eof
                and not pipe.to_sock and not pipe.to_peer):
            pipe.sock.close()
            pipe.sock = None
        if pipe.sock is None and pipe.failed and pipe.mode == "" and not pipe.told:
            continue
        if pipe.sock is None and pipe.failed:
            _meter_close(sid, "ошибка")
            pipes.pop(sid, None)
            continue
        if pipe.sock is None and pipe.local_eof and pipe.remote_eof and pipe.end_sent:
            _meter_close(sid, "закрыт")
            pipes.pop(sid, None)


def _reject(pipe: Pipe) -> None:
    reason = pipe.failed.encode("utf-8", errors="replace")
    msg = (b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: %d\r\n"
           b"Connection: close\r\n\r\n" % len(reason)) + reason
    try:
        pipe.sock.sendall(msg)
    except OSError:
        pass
    pipe.sock.close()
    pipe.sock = None


def _greet(pipe: Pipe) -> None:
    """Сразу сказать клиенту, что туннель есть.

    Удалённый TCP ещё может соединяться. Байты клиента копятся и уйдут,
    когда хост откроет сокет. Иначе VS Code не ждёт ответ прокси и лимиты
    остаются «Недоступно».
    """
    if pipe.mode != "connect" or pipe.greeted or pipe.sock is None:
        return
    try:
        pipe.sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    except OSError:
        pipe.local_eof = True
        return
    pipe.greeted = True
    if pipe.early:
        pipe.to_peer.extend(pipe.early)
        pipe.early = b""


def _abandon(pipe: Pipe) -> None:
    """Закрыть поток и сообщить второй стороне через END, не открывая его заново."""
    pipe.opened = True
    pipe.saw_peer = True
    pipe.local_eof = True
    pipe.remote_eof = True
    pipe.to_peer.clear()
    pipe.to_sock.clear()
    pipe.end_sent = False
    if pipe.sock is not None:
        try:
            pipe.sock.close()
        except OSError:
            pass
        pipe.sock = None


def _reap(pipes: dict[int, Pipe]) -> None:
    """Закрыть поток, по которому 20 с нет нового байта.

    Иначе оборванное рукопожатие держит единственные слоты, опрос
    привязки получает отказ сразу, и кнопка в лимитах ничего не меняет.
    """
    now = time.monotonic()
    for pipe in list(pipes.values()):
        if pipe.sock is None:
            continue
        warm = pipe.opened and pipe.read_n > 0 and pipe.wrote_n > 0
        if now - pipe.touch <= (_WARM_S if warm else _STALL_S):
            continue
        if warm:
            app.warn(f".. поток {pipe.sid} тихий, закрываю")
            _meter_close(pipe.sid, "тихий, закрыт")
        else:
            app.warn(f".. поток {pipe.sid} молчит, закрываю")
            _meter_close(pipe.sid, "молчит, закрыт")
        _abandon(pipe)


def _live(pipes: dict[int, Pipe]) -> list[Pipe]:
    return [
        pipe for pipe in pipes.values()
        if pipe.sock is not None and not (pipe.local_eof and pipe.remote_eof)
    ]


def pump(pipes: dict[int, Pipe], dial: bool) -> None:
    _reap(pipes)
    for pipe in list(pipes.values()):
        if dial and pipe.opened:
            _pull(pipe)
        elif not dial and (pipe.mode != "connect" or pipe.greeted):
            _pull(pipe)
        _flush(pipe)
        if not dial:
            _greet(pipe)
    _close_done(pipes)


def _pending_bytes(pipes: dict[int, Pipe]) -> int:
    return sum(max(0, pipe.base + len(pipe.to_peer) - pipe.offered) for pipe in pipes.values())


def _snapshot_full(pipes: dict[int, Pipe], cells: int = GATE_CELLS_MIN) -> bool:
    return _pending_bytes(pipes) >= (cells - 2) * DATA_MAX


def _hold_small(pipes: dict[int, Pipe], dial: bool, cells: int = GATE_CELLS_MIN) -> None:
    """Подождать мелкие куски, если снимок ещё не полный."""
    if not _has_tail(pipes) or _snapshot_full(pipes, cells):
        return
    end = time.monotonic() + _COALESCE_S
    while time.monotonic() < end and not _snapshot_full(pipes, cells):
        pump(pipes, dial=dial)
        time.sleep(0.02)


def gather(pipes: dict[int, Pipe], pause: float, cells: int = GATE_CELLS_MIN) -> None:
    """Забрать байты сокета. Первый кусок подождать ещё 150 мс, полный снимок — нет."""
    end = time.monotonic() + pause
    first = None
    while time.monotonic() < end:
        pump(pipes, dial=True)
        if _snapshot_full(pipes, cells):
            return
        if _pending_bytes(pipes) > 0:
            if first is None:
                first = time.monotonic()
            if time.monotonic() - first >= _COALESCE_S:
                pump(pipes, dial=True)
                return
        time.sleep(0.01)


def _remember_gate(transport, frames, pipes: dict[int, Pipe]) -> None:
    """Цифры гуеста перед снимком. На хосте доски нет, вызов пустой."""
    board = getattr(transport, "board", None)
    if board is None:
        return
    live = 0
    for pipe in pipes.values():
        if pipe.sock is None or pipe.failed:
            continue
        if pipe.local_eof and pipe.remote_eof:
            continue
        live += 1
    board.note(len(frames), _backlog(pipes), live)


def _stat_fail(transport) -> None:
    board = getattr(transport, "board", None)
    if board is None:
        return
    from .statcell import ERR_SEND
    board.fail(ERR_SEND)


def send_snapshot(transport, frames: list[bytes], limit: int | None = None) -> str | None:
    face = monitor.current()
    if face is not None and frames:
        face.channel(out_n=sum(len(raw) for raw in frames))
    if hasattr(transport, "_nudge_once"):
        transport._nudge_once = True
    many = getattr(transport, "send_many", None)
    if many is None:
        return app.send_frames(transport, frames)
    from .clipboard import ClipboardError
    try:
        many(frames, limit=limit or GATE_CELLS_MIN)
    except ClipboardError as exc:
        return str(exc)
    return None


def recv_snapshot(transport, first_wait: float, idle: float = 0.05):
    frame, err = app.receive_frame(transport, first_wait)
    if err is not None:
        app.warn(f"?? кадр шлюза испорчен ({err})")
    if frame is None:
        return []
    got = [frame]
    while len(got) < GATE_CELLS_MAX:
        frame, err = app.receive_frame(transport, idle)
        if frame is None:
            break
        got.append(frame)
    face = monitor.current()
    if face is not None and got:
        face.channel(in_n=sum(len(frame.payload) for frame in got))
    return got


def _begin_connect(pipe: Pipe) -> None:
    """Начать TCP, не останавливая ответы по другим потокам. Только IPv4: AAAA часто молчит."""
    host, port = pipe.target
    _meter_open(pipe.sid, f"{host}:{port}")
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        pipe.failed = str(exc)
        app.warn(f"?? tcp {host}:{port}: {exc}")
        return
    if not infos:
        pipe.failed = "нет IPv4"
        app.warn(f"?? tcp {host}:{port}: нет IPv4")
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    err = sock.connect_ex(infos[0][4])
    if err not in _PENDING:
        pipe.failed = socket.error(err).strerror or str(err)
        app.warn(f"?? tcp {host}:{port}: {pipe.failed}")
        sock.close()
        return
    pipe.sock = sock
    if err == 0:
        pipe.opened = True
        app.say(f">> tcp {host}:{port} поток {pipe.sid}")
        _meter_ready(pipe.sid)
        return
    pipe.connecting = True
    pipe.connect_at = time.monotonic()


def _finish_connect(pipes: dict[int, Pipe]) -> None:
    pending = [p for p in pipes.values() if p.connecting and p.sock is not None]
    if not pending:
        return
    socks = [p.sock for p in pending]
    try:
        _, writable, failed = select.select([], socks, socks, 0)
    except (OSError, ValueError):
        writable, failed = [], socks
    now = time.monotonic()
    ready = set(writable) | set(failed)
    for pipe in pending:
        host, port = pipe.target
        if pipe.sock not in ready:
            if now - pipe.connect_at > _CONNECT_WAIT:
                pipe.connecting = False
                pipe.failed = "timed out"
                app.warn(f"?? tcp {host}:{port}: timed out")
                pipe.sock.close()
                pipe.sock = None
            continue
        err = pipe.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        pipe.connecting = False
        if err:
            pipe.failed = socket.error(err).strerror or str(err)
            app.warn(f"?? tcp {host}:{port}: {pipe.failed}")
            pipe.sock.close()
            pipe.sock = None
            continue
        pipe.opened = True
        app.say(f">> tcp {host}:{port} поток {pipe.sid}")
        _meter_ready(pipe.sid)


def _dial_status(pipes: dict[int, Pipe], seq: app.SeqCounter) -> list[bytes]:
    extra = []
    for pipe in pipes.values():
        if pipe.mode != "":
            continue
        if pipe.failed:
            pipe.told = True
            extra.append(_frame(seq, OP_FAIL, pipe.sid, pipe.failed.encode("utf-8", errors="replace")[:180]))
        elif pipe.opened and not pipe.saw_peer:
            extra.append(_frame(seq, OP_OK, pipe.sid))
    return extra


def _has_tail(pipes: dict[int, Pipe]) -> bool:
    """В сокет уже прочитан кусок, который не влез в прошлый снимок."""
    return any(p.base + len(p.to_peer) > p.offered for p in pipes.values())


def _emit(transport, pipes: dict[int, Pipe], seq: app.SeqCounter, poll: bool,
          bias: Bias | None = None, ceiling: Ceiling | None = None) -> None:
    if ceiling is not None:
        ceiling.observe(pipes)
    yield_turn = bool(bias is not None and bias.should_yield(pipes))
    cells = ceiling.cells if ceiling is not None else GATE_CELLS_MIN
    status = _dial_status(pipes, seq)
    out = build_frames(pipes, seq, drive=False, reserve=len(status), yield_turn=yield_turn,
                       cells=cells)
    if not yield_turn:
        room = cells - len(out)
        for raw in status:
            if room <= 0:
                break
            out.append(raw)
            room -= 1
    if not out:
        if not poll:
            return
        out = [_frame(seq, OP_POLL, 0)]
    if bias is not None:
        bias.note_sent(out, yield_turn and any(_is_yield(raw) for raw in out))
    _remember_gate(transport, out, pipes)
    fail = send_snapshot(transport, out, limit=cells)
    if ceiling is not None and not yield_turn and _backlog(pipes) > 0:
        ceiling.arm(pipes, transport)
    if transport.yielding:
        return
    if fail is not None:
        _stat_fail(transport)
        app.warn(f"?? ответ шлюза не ушёл: {fail}")


def dial_loop(transport, stop, wait: float, face: monitor.Face | None = None) -> None:
    monitor.bind(face)
    _arm_pause(transport)
    pipes: dict[int, Pipe] = {}
    seq = app.SeqCounter()
    bias = Bias()
    ceiling = Ceiling()
    while not stop.is_set():
        _meter_hold(transport)
        _reap(pipes)
        if _human_quiet(transport):
            now = time.monotonic()
            for pipe in pipes.values():
                pipe.touch = now
            frames = recv_snapshot(transport, 0.2)
            if frames:
                bias.note_peer(frames)
                apply_frames(pipes, frames, dial=True, transport=transport)
                ceiling.observe(pipes)
            continue
        pause = 0.15 if _has_tail(pipes) else (0.3 if stop.is_set() else wait)
        frames = recv_snapshot(transport, pause)
        if not frames:
            _finish_connect(pipes)
            ceiling.observe(pipes)
            if _has_tail(pipes) and not transport.paused:
                gather(pipes, 0.05, ceiling.cells)
                _emit(transport, pipes, seq, poll=False, bias=bias, ceiling=ceiling)
            continue
        bias.note_peer(frames)
        apply_frames(pipes, frames, dial=True, transport=transport)
        ceiling.observe(pipes)
        if transport.paused:
            continue
        _finish_connect(pipes)
        end = time.monotonic() + 0.5
        while time.monotonic() < end and any(p.connecting for p in pipes.values()):
            time.sleep(0.02)
            _finish_connect(pipes)
        gather(pipes, 0.3, ceiling.cells)
        _finish_connect(pipes)
        _emit(transport, pipes, seq, poll=True, bias=bias, ceiling=ceiling)


def _read_head(conn: socket.socket) -> tuple[bytes, bytes] | None:
    buf = b""
    while b"\r\n\r\n" not in buf:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            return None
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
        if len(buf) > 65536:
            return None
    head, rest = buf.split(b"\r\n\r\n", 1)
    return head, rest


def _parse_request(head: bytes, rest: bytes):
    """CONNECT или обычный прокси-запрос. None — ответить 400."""
    lines = head.split(b"\r\n")
    if not lines or len(lines[0].split(b" ")) != 3:
        return None
    method, url, _ver = lines[0].split(b" ")
    if method.upper() == b"CONNECT":
        hostport = url.decode("ascii", errors="replace")
        if hostport.startswith("["):
            return None
        host, _, port_s = hostport.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            return None
        return "connect", host, port, b"", rest
    try:
        parts = urlsplit(url.decode("ascii", errors="replace"))
    except ValueError:
        return None
    if parts.scheme not in ("http", "") or not parts.hostname:
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    headers = b"\r\n".join(lines[1:])
    rewritten = method + b" " + path.encode("ascii") + b" HTTP/1.1\r\n" + headers + b"\r\n\r\n" + rest
    return "http", parts.hostname, port, rewritten, b""


def _wait_reply(transport, srv, pipes: dict[int, Pipe], nxt: int, total: float):
    """Ждать снимок и при этом принимать новые соединения прокси.

    Нового клиента не держим до конца ожидания: иначе VS Code не дожидается
    ответа прокси и «Повторить» у лимитов ничего не меняет.
    """
    end = time.monotonic() + total
    frames = []
    fresh = False
    waiting = {
        pipe.sid for pipe in pipes.values()
        if pipe.mode and not pipe.opened and not pipe.failed
    }
    while True:
        nxt = _accept(srv, pipes, nxt)
        pump(pipes, dial=False)
        if frames:
            break
        if any(
            pipe.sid not in waiting and pipe.mode and not pipe.opened and not pipe.failed
            for pipe in pipes.values()
        ):
            fresh = True
            break
        left = end - time.monotonic()
        if left <= 0:
            break
        got = recv_snapshot(transport, min(0.1, left))
        if got:
            frames = got
    return nxt, frames, fresh


def _tunnel_host(host: str) -> bool:
    """В слот идут только Кода и локальные проверки.

    Иначе VS Code тащит через буфер cdn, телеметрию и github, и проверка
    «Кода онлайн» не успевает за свой таймер.
    """
    name = host.lower().rstrip(".")
    if name in ("localhost", "127.0.0.1"):
        return True
    return name == "kodacode.ru" or name.endswith(".kodacode.ru")


def _make_room(pipes: dict[int, Pipe], host: str) -> bool:
    """Один поток на весь буфер. Проверка api вытесняет застрявшую авторизацию."""
    if len(_live(pipes)) < MAX_STREAMS:
        return True
    if host != "api.kodacode.ru":
        return False
    others = [pipe for pipe in _live(pipes) if not (pipe.target and pipe.target[0] == "api.kodacode.ru")]
    if not others:
        return False
    victim = min(others, key=lambda pipe: pipe.touch)
    app.warn(f".. поток {victim.sid} уступает проверке Коды")
    _abandon(victim)
    return len(_live(pipes)) < MAX_STREAMS


def _accept(srv: socket.socket, pipes: dict[int, Pipe], nxt: int) -> int:
    try:
        conn, _addr = srv.accept()
    except BlockingIOError:
        return nxt
    except OSError:
        return nxt
    conn.settimeout(5)
    got = _read_head(conn)
    try:
        conn.setblocking(False)
    except OSError:
        conn.close()
        return nxt
    if got is None:
        conn.close()
        return nxt
    parsed = _parse_request(*got)
    if parsed is None:
        try:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        conn.close()
        return nxt
    mode, host, port, initial, early = parsed
    if not _tunnel_host(host):
        try:
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        conn.close()
        return nxt
    if not _make_room(pipes, host):
        try:
            conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        conn.close()
        return nxt
    pipe = Pipe(nxt)
    pipe.sock = conn
    pipe.mode = mode
    pipe.target = (host, port)
    pipe.to_peer.extend(initial)
    pipe.early = early
    pipes[nxt] = pipe
    app.say(f"<< прокси {mode} {host}:{port} поток {nxt}")
    _meter_open(nxt, f"{host}:{port}")
    return nxt + 1


def listen_loop(transport, proxy_host: str, proxy_port: int, stop, wait: float,
                 face: monitor.Face | None = None) -> None:
    from .statcell import FEED_NET, attach_guest
    attach_guest(transport, FEED_NET)
    monitor.bind(face)
    _arm_pause(transport)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((proxy_host, proxy_port))
    srv.listen(8)
    srv.setblocking(False)
    app.say(f"прокси http://{proxy_host}:{proxy_port}")
    pipes: dict[int, Pipe] = {}
    seq = app.SeqCounter(1)
    bias = Bias()
    ceiling = Ceiling()
    nxt = 1
    try:
        while not stop.is_set():
            nxt = _accept(srv, pipes, nxt)
            pump(pipes, dial=False)
            waiting = any(p.target and not p.failed and not (p.local_eof and p.remote_eof) for p in pipes.values())
            if _human_quiet(transport):
                now = time.monotonic()
                for pipe in pipes.values():
                    pipe.touch = now
                reply = recv_snapshot(transport, 0.2)
                if reply:
                    bias.note_peer(reply)
                    apply_frames(pipes, reply, dial=False, transport=transport)
                    ceiling.observe(pipes)
                continue
            ceiling.observe(pipes)
            yield_turn = bias.should_yield(pipes)
            if not yield_turn:
                _hold_small(pipes, dial=False, cells=ceiling.cells)
            frames = build_frames(pipes, seq, drive=waiting, yield_turn=yield_turn,
                                  cells=ceiling.cells)
            if not frames:
                reply = recv_snapshot(transport, 0.05)
                if reply:
                    bias.note_peer(reply)
                    apply_frames(pipes, reply, dial=False, transport=transport)
                    ceiling.observe(pipes)
                else:
                    time.sleep(0.05)
                continue
            bias.note_sent(frames, yield_turn and any(_is_yield(raw) for raw in frames))
            _remember_gate(transport, frames, pipes)
            fail = send_snapshot(transport, frames, limit=ceiling.cells)
            if not yield_turn and _backlog(pipes) > 0:
                ceiling.arm(pipes, transport)
            if transport.yielding:
                continue
            if fail is not None:
                _stat_fail(transport)
                if not transport.paused:
                    app.warn(f"?? снимок шлюза не ушёл: {fail}")
                    time.sleep(0.2)
                continue
            nxt, reply, fresh = _wait_reply(transport, srv, pipes, nxt, wait)
            if not reply:
                if fresh or transport.paused:
                    continue
                for pipe in pipes.values():
                    pipe.end_sent = False
                app.warn(".. тишина шлюза, повторю снимок")
                ceiling.observe(pipes)
                continue
            bias.note_peer(reply)
            apply_frames(pipes, reply, dial=False, transport=transport)
            ceiling.observe(pipes)
            for pipe in pipes.values():
                if pipe.failed:
                    _reject(pipe)
                else:
                    _greet(pipe)
            pump(pipes, dial=False)
    finally:
        srv.close()
        for pipe in pipes.values():
            if pipe.sock is not None:
                pipe.sock.close()


def _start_monitor(args):
    """Страница на этой машине. Пустой --monitor оставляет морду выключенной."""
    raw = (getattr(args, "monitor", "") or "").strip()
    if not raw:
        return None
    host, sep, port_s = raw.rpartition(":")
    if not sep:
        app.warn(f"!! монитор: нужен адрес вида 127.0.0.1:8078, получено {raw}")
        return None
    try:
        port = int(port_s)
    except ValueError:
        app.warn(f"!! монитор: порт не число в {raw}")
        return None
    who = args.peer_id or ""
    if args.mode == "dial":
        label = f"эта машина, я {who}" if who else "эта машина"
    else:
        label = f"прокси, я {who}" if who else "прокси"
    face = monitor.Face(args.mode, label)
    monitor.serve(face, host or "127.0.0.1", port)
    return face


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rdpconn.gate",
        description="Локальный прокси по каналу буфера: listen на машине без сети, dial на машине с интернетом.",
    )
    p.add_argument("mode", choices=("listen", "dial"))
    app.add_channel_args(p)
    p.add_argument("--proxy", default="127.0.0.1:8079",
                   help="адрес локального прокси для listen (по умолчанию 127.0.0.1:8079)")
    p.add_argument("--monitor", default="",
                   help="страница на этой машине, например 127.0.0.1:8078. Пусто — выключено")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        channel = app.open_channel(args)
    except app.UsageError as exc:
        app.warn(str(exc))
        return 2
    app.say(f"канал: {channel.label}")
    app.peer_hint(args)
    store = getattr(channel.transport, "_store", None)
    if store is not None and store.__class__.__name__ == "WindowsClipboard" and args.wait < 8:
        args.wait = 8
        app.say(".. жду ответ шлюза до 8 с: повтор снимка на редиректе длиннее секунды")
    stop = __import__("threading").Event()
    face = _start_monitor(args)
    try:
        if args.mode == "dial":
            dial_loop(channel.transport, stop, args.wait, face)
        else:
            host, _, port_s = args.proxy.rpartition(":")
            listen_loop(channel.transport, host or "127.0.0.1", int(port_s), stop, args.wait, face)
    except KeyboardInterrupt:
        stop.set()
        app.say("")
    finally:
        channel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
