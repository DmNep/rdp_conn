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
import hashlib
import json
import os
import select
import shutil
import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import app, clipboard, config, fileclip, framing, monitor

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
# Крупный кусок уходит файлом, не ячейками (docs/roadmap.md, пункт 5) — см.
# "bulk"-блок ниже. OFFER несёт (offset, length, sha256) в body, не в стандартных
# полях offset/payload кадра: это анонс, а не сами данные, формат кадра не
# меняется. ABORT откатывает получателя или отправителя обратно на ячейки, не
# дожидаясь полного таймаута другой стороны.
OP_BULK_OFFER = 11
OP_BULK_ABORT = 12
_HEAD = 9  # magic + op + id + offset
DATA_MAX = config.MAX_PAYLOAD - _HEAD
MAX_STREAMS = 2
# Выше этого — крупный кусок выгоднее слать файлом (fileclip), не ячейками:
# измерено на живом стенде 27.11.2026 (память project_koda_over_channel.md) —
# текст на пике 13–26 КБ/с, fileclip устойчиво 5–6 МБ/с, но фиксированная
# цена одного файлового переноса (COM: OleSetClipboard/Flush/GetClipboard,
# ретраи на CLIPBRD_E_CANT_OPEN/DV_E_FORMATETC) — от ~20 мс до нескольких
# секунд на один перенос. Ниже порога эта цена не окупается мелким объёмом
# (802 байта сегодня шли эффективных ~34 КБ/с — не быстрее пика текста).
# ОБЯЗАН быть меньше _WINDOW (строка ниже по файлу): _pull() не даёт
# pipe.to_peer вырасти выше _WINDOW+8192, и кандидат в _bulk_round()
# (`len(to_peer) >= BULK_THRESHOLD`) иначе никогда не выполняется — не
# домыслено, а найдено живым прогоном G6 28.11.2026: 300 КБ дошли целиком
# через обычные ячейки при старом значении 128 КБ ровно по этой причине
# (см. память gate_bulk_window_threshold_bug.md). 48 КБ — ниже _WINDOW
# с запасом, ближе к нижней границе «окупается», чем к верхней.
BULK_THRESHOLD = 48 * 1024
# Не передавать больше этого одним файлом: дольше перенос — дольше буфер
# монополизирован (сериализовано, см. docs/roadmap.md 5 — решение владельца:
# не параллелить файловые переносы), и второй поток дольше ждёт своей очереди.
BULK_CHUNK_MAX = 2 * 1024 * 1024
# Таймаут ожидания: короче нельзя — живой стенд 27.11.2026 показал B→A лаг
# свыше 120 с даже на голом tx/rx (память live_check_roadmap_1_4_27nov.md).
# Не измерено живьём для файлового режима конкретно — консервативно взят
# тот же порядок величины, что и у остальных долгих таймаутов канала.
BULK_ACK_WAIT_S = 180.0
BULK_WATCH_WAIT_S = 180.0
# Отступ после неудачи (отказ fileclip, таймаут, abort, испорченный файл):
# без него неудача за неудачей душит и файловый путь, и обычный — ни одна
# попытка не успевает отдать байты текстом, пока buld_round снова и снова
# хватает тот же поток под файл. Текст в отступе работает как обычно.
BULK_COOLDOWN_S = 30.0
# fileclip.py's свои дефолты (15 попыток × 0.1 с ≈ 1.5 с) калиброваны по
# её собственным прежним живым тестам (file-FileClipboard, F:\rdp-lab) —
# но живой прогон G6 28.11.2026 (трассировка, gate_bulk_oleSetClipboard_hang.md)
# показал: вызванные ИЗНУТРИ bulk-режима gate, list_clipboard_files()/
# recv_file() стабильно падали HRESULT 0x80040064 все 15 попыток подряд,
# не единожды. Не чинить фикс fileclip.py-дефолты (другие вызывающие —
# CLI, host/guest --drop — на этих значениях уже подтверждены живьём),
# а дать именно bulk-режиму свой, намного более терпеливый бюджет:
# тот же шаг (0.1 с), но в 10 раз больше попыток — тот же порядок
# величины, что и задокументированный лаг обычного кругового обмена на
# живом редиректе («до 8 с»), с запасом. Не измерено, достаточно ли
# этого — следующий живой прогон покажет.
BULK_FILE_RECV_RETRIES = 150
BULK_FILE_RECV_RETRY_WAIT_S = 0.1
# Рукопожатие TLS через буфер занимает около 8 с. Пачка параллельных
# соединений не успевает ни одно, слоты остаются заняты, и проверка
# «Кода онлайн» больше не стартует.
_STALL_S = 20.0
# Оба направления уже сходили: это живой туннель, а не оборванное рукопожатие.
# Его можно держать тихим, чтобы следующий запрос Коды не начинал TLS заново.
_WARM_S = 180.0
# Повтор снимка на тишине (listen_loop) растёт от wait, а не идёт тем же
# тактом бесконечно: любая публикация — хоть с тем же содержимым — это для
# редиректа новое событие (WindowsClipboard.seq() растёт на любую запись,
# docs/protocol.md 5), и повтор чаще настоящего круга туда-обратно перехватывает
# свою же публикацию прямо перед тем, как она должна была дойти. Потолок ниже
# _WARM_S: это активная сессия, а не фоновый heartbeat, держать её безответной
# минутами не стоит.
_SILENCE_BACKOFF_MAX_S = 60.0
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
# Верхняя граница, до которой _pull() копит pipe.to_peer (flow control на
# поток), а не порог для bulk-режима — тот ниже, см. BULK_THRESHOLD выше по
# файлу: оно обязано быть меньше этого числа, иначе кандидат на файловую
# передачу никогда не наберётся.
_WINDOW = 64 * 1024
# Без этой проверки правка одной константы без другой тихо возвращает баг
# 28.11.2026 (найден живым прогоном G6, память gate_bulk_window_threshold_
# bug.md): to_peer не растёт выше _WINDOW, и кандидат в _bulk_round() на
# файловый режим никогда не наберётся, если порог не строго ниже окна.
assert BULK_THRESHOLD < _WINDOW, "BULK_THRESHOLD обязан быть меньше _WINDOW"
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
        # Файловый режим одного крупного куска (docs/roadmap.md 5) — не
        # больше одного из двух одновременно, не по обоим сразу: этот конец
        # либо шлёт файл для этого потока (bulk_out), либо ждёт чужой
        # (bulk_in), никогда оба на одном Pipe.
        self.bulk_out: "_BulkOut | None" = None
        self.bulk_in: "_BulkIn | None" = None
        # После любой неудачи файлового переноса — не пробовать этот же
        # поток файлом заново тут же (иначе неудача за неудачей душит и
        # файловый путь, и обычный текстовый — ничего не доходит вовсе).
        # В отступе build_frames() как ни в чём не бывало режет те же байты
        # ячейками — тексту отступ не нужен и не виден.
        self.bulk_cooldown_until = 0.0


@dataclass
class _BulkOut:
    """Мы анонсировали OP_BULK_OFFER и ждём, когда base сдвинется на offset+length
    (обычным OP_ACK, который и так понимает apply_frames) — или по таймауту
    откатываемся: to_peer/base/offered никто не трогал, build_frames сам
    продолжит эти же байты ячейками."""

    offset: int
    length: int
    digest: str
    path: str
    sent_at: float


@dataclass
class _BulkIn:
    """Увидели чужой OP_BULK_OFFER на offset <= local_got — значит в файле
    либо ровно продолжение (offset == local_got), либо голова файла
    повторяет то, что уже дошло ячейками (offset < local_got, отправитель
    анонсировал от base, не от того, что он ЛОКАЛЬНО считает уже
    отправленным) — то и другое принимаем, skip просто обрезает
    известную голову перед тем, как дописать в to_sock. offset > local_got
    была бы дырой — такой анонс здесь не окажется, apply_frames его не
    создаёт."""

    offset: int
    length: int
    digest: str
    seen_at: float
    skip: int


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


def _pack_bulk_offer(offset: int, length: int, digest_hex: str) -> bytes:
    """offset(4) + length(4) + sha256(32 сырых байт, не hex) — анонс, не
    данные: OP_BULK_OFFER не входит в (OP_DATA, OP_ACK), поэтому pack()
    кладёт это целиком в body, не трогая offset-поле кадра."""
    return offset.to_bytes(4, "big") + length.to_bytes(4, "big") + bytes.fromhex(digest_hex)


def _unpack_bulk_offer(body: bytes) -> tuple[int, int, str] | None:
    if len(body) != 40:
        return None
    offset = int.from_bytes(body[:4], "big")
    length = int.from_bytes(body[4:8], "big")
    return offset, length, body[8:40].hex()


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


# Кэш последнего достигнутого потолка (docs/roadmap.md, пункт 4: "старт на
# откалиброванных параметрах"). Активный upfront-замер потолка ячеек, как
# у tune.py для паузы, здесь сознательно не сделан: он требовал бы, чтобы
# ОБЕ стороны уже отвечали на пробные кадры ДО входа в listen_loop/dial_loop
# — новая двусторонняя синхронизация именно того типа, что уже один раз
# подвела coder host/guest на живом стенде (C1-C6, память
# live_acceptance_gate_coder_fileshake_27nov.md). Вместо этого — то, что
# подтверждено реальной передачей в ПРЕДЫДУЩЕМ запуске того же peer_id,
# переживает перезапуск процесса: холодный старт с GATE_CELLS_MIN остаётся,
# только когда и подтверждённой истории ещё не было.
_CEILING_DIR = os.path.join(tempfile.gettempdir(), "rdpconn-ceiling")


def _ceiling_cache_path(peer_id: str | None) -> str:
    label = "".join(c if c.isalnum() or c in "-_." else "_" for c in (peer_id or "no-peer-id"))
    return os.path.join(_CEILING_DIR, f"{label}.json")


def _load_cached_ceiling(peer_id: str | None) -> int | None:
    try:
        with open(_ceiling_cache_path(peer_id), encoding="utf-8") as f:
            cells = int(json.load(f)["cells"])
    except (OSError, ValueError, KeyError):
        return None
    return cells if GATE_CELLS_MIN <= cells <= GATE_CELLS_MAX else None


def _save_cached_ceiling(peer_id: str | None, cells: int) -> None:
    try:
        os.makedirs(_CEILING_DIR, exist_ok=True)
        with open(_ceiling_cache_path(peer_id), "w", encoding="utf-8") as f:
            json.dump({"cells": cells}, f)
    except OSError:
        pass  # кэш — это только ускорение следующего старта, не часть протокола


class Ceiling:
    """Сколько ячеек класть в свой снимок.

    Вверх — только полный снимок, который подтвердили. Обычный повтор
    через 0.94 с потерей не считается. Одна потеря минус 6, вторая подряд
    без чистого полного снимка — сразу на 40 и до следующей паузы.
    """

    def __init__(self, peer_id: str | None = None) -> None:
        self.peer_id = peer_id
        self.cells = _load_cached_ceiling(peer_id) or GATE_CELLS_MIN
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
        _save_cached_ceiling(self.peer_id, self.cells)
        app.say(f".. потолок {old} → {self.cells}, полный снимок подтверждён")

    def _drop(self) -> None:
        old = self.cells
        self.clear()
        if self.strikes >= 1:
            self.cells = GATE_CELLS_MIN
            self.allow = False
            self.quiet_since = None
            self.strikes = 0
            _save_cached_ceiling(self.peer_id, self.cells)
            app.say(f".. потолок {old} → {self.cells}, вторая потеря, жду новую передачу")
            return
        self.strikes = 1
        self.cells = max(GATE_CELLS_MIN, old - GATE_CELLS_STEP)
        if self.cells == GATE_CELLS_MIN:
            self.allow = False
            self.quiet_since = None
        _save_cached_ceiling(self.peer_id, self.cells)
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
        elif op == OP_BULK_OFFER:
            pipe.saw_peer = True
            pipe.touch = time.monotonic()
            parsed = _unpack_bulk_offer(body)
            if parsed is None:
                continue
            offer_offset, length, digest = parsed
            app.trace("gate-offer-seen", sid=pipe.sid, offer_offset=offer_offset,
                      length=length, local_got=pipe.local_got, bulk_in=pipe.bulk_in is not None)
            if pipe.bulk_in is None and offer_offset <= pipe.local_got:
                pipe.bulk_in = _BulkIn(offset=offer_offset, length=length,
                                       digest=digest, seen_at=time.monotonic(),
                                       skip=pipe.local_got - offer_offset)
                # Симметрично dial-стороне (_offer_bulk): свой же
                # отложенный nudge классическим SetClipboardData
                # способен стереть ПРИХОДЯЩИЙ файл из буфера этой
                # стороны не хуже, чем отправительский — отменить его,
                # пока идёт приём.
                if transport is not None:
                    cancel_nudge = getattr(transport, "cancel_nudge", None)
                    if cancel_nudge is not None:
                        cancel_nudge()
            # Иначе — либо уже идёт приём, либо offset > local_got (дыра:
            # отправитель анонсировал то, что мы ещё не видели даже
            # ячейками) — не трогаем, отправитель сам поймёт по таймауту
            # ожидания ACK и откатится на ячейки.
        elif op == OP_BULK_ABORT:
            # Абортировать может любая сторона (отправитель — если файл не
            # ушёл в буфер; получатель — если не дождался или файл испорчен),
            # а эта сторона заранее не знает, какая из двух своих ролей у
            # неё сейчас активна для этого потока — снимаем обе.
            if pipe.bulk_out is not None:
                _cleanup_bulk_file(pipe.bulk_out.path)
            pipe.bulk_out = None
            pipe.bulk_in = None


# --- файловый режим крупного куска (docs/roadmap.md, пункт 5) ---------------
#
# Один перенос — это переключатель всего буфера, не боковой канал:
# fileclip.send_files() вызывает OleSetClipboard(), который заменяет текущего
# владельца буфера целиком (см. комментарий в начале fileclip.py) — текстовый
# G1-снимок в этот момент исчезает так же, как исчез бы при обычной вставке
# файла поверх скопированного текста. Поэтому, пока bulk_out/bulk_in у
# какого-то потока выставлен, обычный текстовый круг (_emit/recv_snapshot)
# не идёт вовсе — см. _bulk_round(), вызываемый из dial_loop/listen_loop
# раньше всего остального в теле цикла.
#
# Серилизовано по решению владельца: один файловый перенос на весь процесс
# разом, не по потоку — простая очередь, не отдельный лок на Pipe.


def _cleanup_bulk_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _send_bulk_abort(transport, seq: app.SeqCounter, sid: int) -> None:
    """OP_BULK_ABORT — и settle-пауза вслед за ним, той же природы, что у
    анонса в _offer_bulk(): без неё собственный следующий обычный круг этой
    же стороны переиздаёт свой снимок и стирает только что опубликованный
    abort раньше, чем собеседник успевает его прочитать (его опрос —
    раз в 0.2 с, а локальный MemoryClipboard успевает заново опубликовать
    за микросекунды) — живьём пойман именно так, не домыслено."""
    send_snapshot(transport, [_frame(seq, OP_BULK_ABORT, sid)])
    time.sleep(max(0.5, clipboard.NUDGE_AFTER_S))


def _offer_bulk(transport, pipe: Pipe, seq: app.SeqCounter) -> None:
    """Объявить крупный кусок и переключить буфер на файл.

    Анонс обязан начинаться с pipe.base (уже подтверждено ACK), не с
    pipe.offered (уже отправлено ячейками, но ACK мог и не дойти, а на
    локально-быстром буфере собеседник мог вообще не успеть прочитать
    снимок перед тем, как его перекрыл следующий) — анонс от offered
    молча терялся ровно тогда, когда собеседник в этот момент ещё не
    видел тех самых ячеек: local_got оставался позади offset анонса, и
    по правилу apply_frames (не дыра — не принимать) анонс отбрасывался,
    а не только на живом рассинхроне 28.11.2026 (память
    gate_bulk_oleSetClipboard_hang.md), но и в tools/test_gate_bulk.py на
    MemoryClipboard, где это воспроизводится почти всегда.

    base — единственная величина с гарантией: то, что отправитель НЕ
    ACKнул, собеседник мог как получить (ACK просто не успел прийти), так
    и не получить вовсе (ACK придёт позже) — но base никогда не бывает
    ВПЕРЕДИ того, что собеседник видел. Поэтому хвост, который собеседник
    уже получил ячейками (local_got между base и offer_offset+length),
    принимающая сторона обрезает сама (apply_frames/_receive_bulk,
    tolerant-match по offer_offset <= local_got) — не дыра, а повтор уже
    известного, которого можно просто не дублировать в to_sock.

    Откат при любой накладке — не особый код, а отсутствие изменений в
    to_peer/base до настоящего ACK: при сбое build_frames() на следующем
    же обычном круге сам продолжит байты с pipe.base ячейками, как будто
    файлового режима не было.
    """
    length = min(len(pipe.to_peer), BULK_CHUNK_MAX)
    chunk = bytes(pipe.to_peer[:length])
    digest = hashlib.sha256(chunk).hexdigest()
    offer = _frame(seq, OP_BULK_OFFER, pipe.sid,
                  _pack_bulk_offer(pipe.base, length, digest))
    app.trace("gate-offer-publish", sid=pipe.sid, offset=pipe.base, length=length)
    fail = send_snapshot(transport, [offer])
    if fail is not None:
        app.warn(f"?? анонс файлового куска не ушёл: {fail}")
        return
    # send_snapshot() безусловно взводит nudge (транспорт сам не знает,
    # что этот конкретный кадр — анонс перед файлом, не обычная ячейка).
    # Без отмены nudge на ближайшем receive() классическим
    # SetClipboardData перепишет буфер назад текстом поверх только что
    # установленного OLE-объекта — и совпадёт по времени почти всегда,
    # не изредка: settle ниже и задержка nudge — один и тот же
    # NUDGE_AFTER_S. Живым прогоном 28.11.2026 подтверждено: nudge
    # срабатывал через ~53 мс после записи файла, на каждой попытке
    # (gate_bulk_oleSetClipboard_hang.md).
    cancel_nudge = getattr(transport, "cancel_nudge", None)
    if cancel_nudge is not None:
        cancel_nudge()
    # Дать анонсу время дойти через редирект, прежде чем стереть его файлом —
    # тот же запас, что уже откалиброван для повтора снимка (docs/protocol.md
    # 5). Один статичный sleep() здесь недостаточен: собеседник мог быть
    # занят СВОЕЙ публикацией (ответ на предыдущий обычный круг) именно в
    # момент этой записи и физически не опросить буфер, пока редирект (или
    # наш же следующий локальный write) не сменит содержимое ещё раз —
    # окно длиной в один снимок первой же попытки, живьём поймано
    # 28.11.2026 (gate_bulk_oleSetClipboard_hang.md): offer ушёл и был
    # записан корректно, но у listen'а between двух его опросов буфера
    # номер сразу перескочил на файловый — сам текст анонса ни разу не
    # попал в его окно чтения. Повтор того же анонса безопасен (это не
    # переключение формата, обычный merge с чужими ячейками через
    # _publish_many) — в отличие от nudge ПОСЛЕ записи файла, который и
    # был прошлой находкой этого дня.
    settle = max(0.5, clipboard.NUDGE_AFTER_S)
    app.trace("gate-offer-settle-begin", sid=pipe.sid, settle_s=settle)
    repeat_every = 0.2
    elapsed = 0.0
    while elapsed < settle:
        time.sleep(min(repeat_every, settle - elapsed))
        elapsed += repeat_every
        send_snapshot(transport, [offer])
        if cancel_nudge is not None:
            cancel_nudge()
    app.trace("gate-offer-settle-end", sid=pipe.sid)
    fd, path = tempfile.mkstemp(prefix=f"rdpconn-gate-bulk-{pipe.sid}-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(chunk)
        app.trace("gate-offer-filewrite-begin", sid=pipe.sid)
        fileclip.send_files([path])
        app.trace("gate-offer-filewrite-end", sid=pipe.sid)
    except (OSError, fileclip.ClipboardFileError) as exc:
        app.warn(f"?? поток {pipe.sid}: файл не ушёл в буфер ({exc}), откат на ячейки")
        _cleanup_bulk_file(path)
        pipe.bulk_cooldown_until = time.monotonic() + BULK_COOLDOWN_S
        # Получатель уже видел анонс — без этого он прождал бы полный таймаут.
        _send_bulk_abort(transport, seq, pipe.sid)
        return
    pipe.bulk_out = _BulkOut(offset=pipe.base, length=length, digest=digest,
                            path=path, sent_at=time.monotonic())
    app.say(f".. поток {pipe.sid}: {length} байт файлом вместо ячеек")


def _wait_bulk_ack(transport, pipes: dict[int, Pipe], pipe: Pipe,
                   seq: app.SeqCounter, dial: bool) -> None:
    """Один тик ожидания подтверждения уже отправленного файла.

    Подтверждение — обычный OP_ACK (apply_frames его уже умеет: сдвигает
    base, обрезает to_peer) на offset+length; отдельного кода «готово» не
    нужно. pump() внутри цикла — только локальные сокеты, не трогает
    транспорт: второй поток продолжает копить/отдавать байты локально,
    пока этот ждёт файл, даже если сам обмен снимками сейчас не идёт.
    """
    state = pipe.bulk_out
    pump(pipes, dial=dial)
    # Не дать _reap() посчитать поток тихим во время долгого, но живого
    # ожидания ACK — это не молчание, это другая работа на том же буфере.
    pipe.touch = time.monotonic()
    _reap(pipes)
    if time.monotonic() - state.sent_at > BULK_ACK_WAIT_S:
        app.warn(f".. поток {pipe.sid}: файл не подтверждён за {BULK_ACK_WAIT_S:.0f} с, откат на ячейки")
        _cleanup_bulk_file(state.path)
        pipe.bulk_out = None
        pipe.bulk_cooldown_until = time.monotonic() + BULK_COOLDOWN_S
        return
    frame, err = app.receive_frame(transport, 0.2)
    if err is not None:
        app.warn(f"?? кадр шлюза испорчен ({err})")
        return
    if frame is None:
        return
    apply_frames(pipes, [frame], dial=dial, transport=transport)
    if pipe.bulk_out is None:
        # OP_BULK_ABORT уже обнулил bulk_out в apply_frames.
        _cleanup_bulk_file(state.path)
        pipe.bulk_cooldown_until = time.monotonic() + BULK_COOLDOWN_S
        return
    if pipe.base >= state.offset + state.length:
        app.say(f".. поток {pipe.sid}: файл подтверждён")
        _cleanup_bulk_file(state.path)
        pipe.bulk_out = None


def _receive_bulk(transport, pipes: dict[int, Pipe], pipe: Pipe,
                  seq: app.SeqCounter, dial: bool) -> None:
    """Принять уже анонсированный файл целиком — блокирующий шаг, не тик.

    Короткие срезы ожидания (не весь BULK_WATCH_WAIT_S одним вызовом),
    чтобы между ними успевать pump() для второго потока, как и в
    _wait_bulk_ack(). Сам приём файла (list_clipboard_files/recv_file) —
    по-прежнему один непрерывный блокирующий шаг, не срезанный на тики:
    второй поток уже не обслуживается во время него, как и раньше (один
    файловый перенос на процесс, решение владельца, см. блок выше), но
    с BULK_FILE_RECV_RETRIES этот шаг теперь может занять заметно дольше
    одной короткой попытки (до ~BULK_FILE_RECV_RETRIES× RETRY_WAIT_S на
    каждый из двух вызовов) — компромисс принят осознанно, не регресс.
    """
    state = pipe.bulk_in
    deadline = state.seen_at + BULK_WATCH_WAIT_S
    since_seq = fileclip.sequence_number()
    app.trace("gate-receive-bulk-enter", sid=pipe.sid, since_seq=since_seq,
             offset=state.offset, length=state.length)
    # Файл мог лечь в буфер ещё ДО того, как мы вообще начали ждать —
    # _offer_bulk пишет файл сразу вслед за анонсом, и наш собственный
    # главный цикл мог добраться сюда позже этого момента. wait_for_new_file
    # ждёт именно СМЕНУ номера с этой секунды — если файл уже лежит, смены
    # больше не будет никогда, и ожидание было бы вечным. Поэтому сначала
    # пробуем забрать как есть, и только если файла ещё нет — ждём смены.
    try:
        fileclip.list_clipboard_files(formatetc_retries=BULK_FILE_RECV_RETRIES,
                                      formatetc_retry_wait=BULK_FILE_RECV_RETRY_WAIT_S)
        already_there = True
    except (OSError, fileclip.ClipboardFileError):
        already_there = False
    while not already_there:
        pump(pipes, dial=dial)
        pipe.touch = time.monotonic()
        _reap(pipes)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            app.warn(f".. поток {pipe.sid}: файл не пришёл за {BULK_WATCH_WAIT_S:.0f} с, прошу откат на ячейки")
            pipe.bulk_in = None
            _send_bulk_abort(transport, seq, pipe.sid)
            return
        # Отправитель мог сам передумать (не ушло в буфер его стороны) —
        # не ждать полный таймаут, если его OP_BULK_ABORT уже пришёл.
        frame, err = app.receive_frame(transport, 0.05)
        if frame is not None:
            apply_frames(pipes, [frame], dial=dial, transport=transport)
            if pipe.bulk_in is None:
                return
        try:
            fileclip.wait_for_new_file(since_seq, timeout=min(2.0, remaining))
            break
        except TimeoutError:
            continue
    tmp_dir = tempfile.mkdtemp(prefix="rdpconn-gate-bulk-recv-")
    try:
        entries = fileclip.list_clipboard_files(formatetc_retries=BULK_FILE_RECV_RETRIES,
                                                formatetc_retry_wait=BULK_FILE_RECV_RETRY_WAIT_S)
        if not entries:
            raise fileclip.ClipboardFileError("пустой список файлов в буфере")
        out_path = os.path.join(tmp_dir, entries[0].name)
        fileclip.recv_file(0, out_path, formatetc_retries=BULK_FILE_RECV_RETRIES,
                           formatetc_retry_wait=BULK_FILE_RECV_RETRY_WAIT_S)
        with open(out_path, "rb") as f:
            data = f.read()
    except (OSError, fileclip.ClipboardFileError) as exc:
        app.warn(f".. поток {pipe.sid}: файл не принят ({exc}), прошу откат на ячейки")
        pipe.bulk_in = None
        _send_bulk_abort(transport, seq, pipe.sid)
        return
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    if len(data) != state.length or hashlib.sha256(data).hexdigest() != state.digest:
        app.warn(f".. поток {pipe.sid}: файл пришёл испорченным ({len(data)} байт, ждали {state.length}), прошу откат на ячейки")
        pipe.bulk_in = None
        _send_bulk_abort(transport, seq, pipe.sid)
        return
    # state.skip — голова файла, которая у нас уже есть ячейками (анонс
    # считался от pipe.base отправителя, не от нашего local_got): дописываем
    # только то, чего действительно не было, иначе задублировали бы байты.
    new_data = data[state.skip:]
    pipe.to_sock.extend(new_data)
    pipe.local_got += len(new_data)
    pipe.saw_peer = True
    pipe.touch = time.monotonic()
    pipe.bulk_in = None
    app.say(f".. поток {pipe.sid}: {len(data)} байт файлом, подтверждаю")
    send_snapshot(transport, [_frame(seq, OP_ACK, pipe.sid, offset=pipe.local_got)])


def _bulk_round(transport, pipes: dict[int, Pipe], seq: app.SeqCounter, dial: bool) -> bool:
    """Если у буфера сейчас дело к файловому каналу — сделать шаг и вернуть
    True (обычный текстовый круг этой итерации пропускается: буфер занят
    файлом или вот-вот будет, см. блок выше). Иначе — False, дальше как обычно.
    """
    incoming = next((p for p in pipes.values() if p.bulk_in is not None), None)
    if incoming is not None:
        _receive_bulk(transport, pipes, incoming, seq, dial)
        return True
    outgoing = next((p for p in pipes.values() if p.bulk_out is not None), None)
    if outgoing is not None:
        _wait_bulk_ack(transport, pipes, outgoing, seq, dial)
        return True
    if _human_quiet(transport):
        return False
    now = time.monotonic()
    candidate = max(
        (p for p in pipes.values()
         if len(p.to_peer) >= BULK_THRESHOLD and now >= p.bulk_cooldown_until),
        key=lambda p: len(p.to_peer), default=None,
    )
    if candidate is not None:
        _offer_bulk(transport, candidate, seq)
        return True
    return False


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
    if pipe.bulk_out is not None:
        _cleanup_bulk_file(pipe.bulk_out.path)
        pipe.bulk_out = None
    pipe.bulk_in = None
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
        age = now - pipe.touch
        if warm:
            app.warn(f".. поток {pipe.sid} тихий, закрываю")
            _meter_close(pipe.sid, "тихий, закрыт")
        else:
            app.warn(f".. поток {pipe.sid} молчит, закрываю")
            _meter_close(pipe.sid, "молчит, закрыт")
        app.trace("gate-pipe-reap", sid=pipe.sid, warm=warm, age=round(age, 2))
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
    ceiling = Ceiling(peer_id=transport.peer_id)
    # В отличие от listen_loop, у dial_loop не было вообще никакой индикации
    # тишины (ни обычной, ни хронической) — до первого кадра от собеседника
    # (даже самого первого OP_OPEN) лог молчал полностью. 27.11.2026 это и
    # произошло на живом стенде: dial не увидел НИ ОДНОГО кадра несколько
    # минут подряд, и в логе не было ни строки — человек не знал, что вообще
    # происходит. Общий со всем остальным каналом порог, app.CHRONIC_STALL_S.
    last_frame_at = time.monotonic()
    chronic_warned = False
    while not stop.is_set():
        _meter_hold(transport)
        _reap(pipes)
        if not chronic_warned and time.monotonic() - last_frame_at >= app.CHRONIC_STALL_S:
            chronic_warned = True
            app.warn(f"!! от собеседника нет ни кадра дольше {app.CHRONIC_STALL_S / 60:.0f} мин — "
                     f"не похоже на обычную паузу редиректа. Если mstsc открыт уже давно, "
                     f"попробуйте переподключить RDP-сессию, прежде чем разбираться дальше")
        if _bulk_round(transport, pipes, seq, dial=True):
            continue
        if _human_quiet(transport):
            now = time.monotonic()
            for pipe in pipes.values():
                pipe.touch = now
            frames = recv_snapshot(transport, 0.2)
            if frames:
                last_frame_at = time.monotonic()
                chronic_warned = False
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
        last_frame_at = time.monotonic()
        chronic_warned = False
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
    ceiling = Ceiling(peer_id=transport.peer_id)
    nxt = 1
    # Растёт на каждой подряд идущей тишине (см. _SILENCE_BACKOFF_MAX_S),
    # сбрасывается в wait, как только пришёл настоящий ответ.
    silence_wait = wait
    # Момент начала текущей серии тишины и было ли уже сказано про
    # возможную деградацию сессии (app.CHRONIC_STALL_S, один раз на серию —
    # не на каждый цикл бэкоффа).
    silence_since: float | None = None
    chronic_warned = False
    try:
        while not stop.is_set():
            nxt = _accept(srv, pipes, nxt)
            pump(pipes, dial=False)
            if _bulk_round(transport, pipes, seq, dial=False):
                continue
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
            sent_at = time.monotonic()
            fail = send_snapshot(transport, frames, limit=ceiling.cells)
            app.trace("gate-send", side="listen", cells=len(frames),
                      bytes=sum(len(raw) for raw in frames), fail=fail is not None)
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
            nxt, reply, fresh = _wait_reply(transport, srv, pipes, nxt, silence_wait)
            if not reply:
                if fresh or transport.paused:
                    continue
                for pipe in pipes.values():
                    pipe.end_sent = False
                app.warn(f".. тишина шлюза {silence_wait:.1f} с, повторю снимок")
                if silence_since is None:
                    silence_since = time.monotonic()
                elif not chronic_warned and time.monotonic() - silence_since >= app.CHRONIC_STALL_S:
                    chronic_warned = True
                    app.warn(f"!! тишина шлюза дольше {app.CHRONIC_STALL_S / 60:.0f} мин без единого "
                             f"признака жизни — не похоже на обычную паузу редиректа. Если mstsc "
                             f"открыт уже давно, попробуйте переподключить RDP-сессию, прежде чем "
                             f"разбираться дальше")
                ceiling.observe(pipes)
                app.trace("gate-silence", side="listen", waited=round(time.monotonic() - sent_at, 2),
                          gap_was=round(silence_wait, 1))
                silence_wait = min(_SILENCE_BACKOFF_MAX_S, silence_wait * 2)
                continue
            app.trace("gate-reply", side="listen", waited=round(time.monotonic() - sent_at, 2),
                      cells=len(reply), bytes=sum(len(frame.payload) for frame in reply))
            silence_wait = wait
            silence_since = None
            chronic_warned = False
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
        channel = app.open_channel(args, role=f"gate-{args.mode}")
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
