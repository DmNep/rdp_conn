"""Текст локальной модели через буфер, без HTTP между сторонами и без порта.

Кадр не меняется: куски лежат в NOTE, длиннее MAX_PAYLOAD не бывают.
Открытый текст сжимается zlib и режется на снимки. Следующий снимок
пишется только после короткого чужого подтверждения — иначе редирект
оставляет запись у автора.

На машине с моделью: ``python -m rdpconn coder host``.
В сессии Cline запускает ``python -m rdpconn coder guest`` и говорит
с ним построчным JSON на stdin/stdout. Слушающего порта нет.
Одновременно со шлюзом этот режим не запускают: слот один.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from . import app, config, framing
from .gate import GATE_CELLS_MIN

MAGIC = b"C1"
OP_PIECE = 1
OP_ACK = 2
OP_END = 3
OP_ERR = 4
FLAG_ZLIB = 0x01
FLAG_WINDOW = 0x02
# Окно само по себе законченный кусок ответа, его можно отдать сразу.
FLAG_ALONE = 0x04
_HEAD = 8
CHUNK = config.MAX_PAYLOAD - _HEAD
CELLS = GATE_CELLS_MIN


class CoderError(RuntimeError):
    pass


def encode(op: int, flags: int, mid: int, part: int, body: bytes) -> bytes:
    if len(body) > CHUNK:
        raise ValueError(f"кусок {len(body)} длиннее {CHUNK}")
    return MAGIC + bytes((op & 0xFF, flags & 0xFF)) + mid.to_bytes(2, "big") + part.to_bytes(2, "big") + body


def decode(payload: bytes):
    if len(payload) < _HEAD or payload[:2] != MAGIC:
        return None
    op = payload[2]
    flags = payload[3]
    mid = int.from_bytes(payload[4:6], "big")
    part = int.from_bytes(payload[6:8], "big")
    return op, flags, mid, part, payload[_HEAD:]


def _note(raw: bytes):
    """Кадр из транспорта — байты. Чужой тип и чужая магия просто не наши."""
    try:
        frame = framing.parse_frame(raw)
    except framing.FrameError:
        return None
    if frame.type != config.NOTE:
        return None
    return decode(frame.payload)


def split_body(body: bytes) -> list[bytes]:
    if not body:
        return [b""]
    return [body[i:i + CHUNK] for i in range(0, len(body), CHUNK)]


class Link:
    """Один конец текстового обмена поверх уже открытого транспорта."""

    def __init__(self, transport, cells: int = CELLS, reply_wait: float = 20.0) -> None:
        self.transport = transport
        self.cells = cells
        self.reply_wait = reply_wait
        self.seq = app.SeqCounter()
        self._mid = 1
        self._queue = 0

    def _next_mid(self) -> int:
        mid = self._mid
        self._mid = 1 if self._mid >= 0xFFFF else self._mid + 1
        return mid

    def _send(self, frames: list[bytes]) -> None:
        if not frames:
            return
        board = getattr(self.transport, "board", None)
        if board is not None:
            board.note(len(frames), getattr(self, "_queue", 0), 1)
        if hasattr(self.transport, "_nudge_once"):
            self.transport._nudge_once = True
        self.transport.send_many(frames, limit=self.cells)

    def _frame(self, op: int, flags: int, mid: int, part: int, body: bytes) -> bytes:
        return framing.build_frame(config.NOTE, self.seq.take(), encode(op, flags, mid, part, body))

    def _windows(self, mid: int, body: bytes, last_op: int, flags_base: int) -> list[list[bytes]]:
        chunks = split_body(body)
        windows: list[list[bytes]] = []
        current: list[bytes] = []
        last = len(chunks) - 1
        for index, chunk in enumerate(chunks):
            end = index == last or len(current) + 1 == self.cells
            op = last_op if index == last else OP_PIECE
            flags = flags_base | (FLAG_WINDOW if end else 0)
            current.append(self._frame(op, flags, mid, index, chunk))
            if end:
                windows.append(current)
                current = []
        return windows

    def _wait_ack(self, mid: int) -> None:
        deadline = time.monotonic() + self.reply_wait
        while time.monotonic() < deadline:
            raw = self.transport.receive(max(0.0, deadline - time.monotonic()))
            if raw is None:
                break
            # Любой кадр — ход жив. Тишина длиннее reply_wait по-прежнему обрыв.
            deadline = time.monotonic() + self.reply_wait
            parsed = _note(raw)
            if parsed and parsed[0] == OP_ACK and parsed[2] == mid:
                return
        raise CoderError("снимок не подтвердили")

    def _write(self, body: bytes, last_op: int, flags_base: int) -> None:
        mid = self._next_mid()
        windows = self._windows(mid, body, last_op, flags_base)
        sizes = [sum(len(frame) for frame in window) for window in windows]
        rest = sum(sizes)
        for index, window in enumerate(windows):
            rest -= sizes[index]
            self._queue = rest
            self._send(window)
            if index + 1 < len(windows):
                self._wait_ack(mid)

    def write_request(self, obj: dict) -> None:
        raw = zlib.compress(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        self._write(raw, OP_END, FLAG_ZLIB)

    def write_reply(self, kind: str, text: str, final: bool) -> None:
        raw = zlib.compress(json.dumps({"k": kind, "s": text}, ensure_ascii=False).encode("utf-8"))
        self._write(raw, OP_END if final else OP_PIECE, FLAG_ZLIB | FLAG_ALONE)

    def write_error(self, text: str) -> None:
        # Ошибка — один кадр. Длинный текст режется: иначе середина уехала бы как кусок ответа.
        self._write(text.encode("utf-8", "replace")[:CHUNK], OP_ERR, 0)

    def _read_window(self, timeout: float | None):
        deadline = None if timeout is None else time.monotonic() + timeout
        parts: list[bytes] = []
        while True:
            if deadline is None:
                left = 5.0
            else:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise CoderError("окно текста не доехало")
            raw = self.transport.receive(left)
            if raw is None:
                if deadline is None:
                    continue
                raise CoderError("окно текста не доехало")
            if timeout is not None:
                deadline = time.monotonic() + timeout
            parsed = _note(raw)
            if parsed is None or parsed[0] == OP_ACK:
                continue
            op, flags, mid, _part, body = parsed
            parts.append(body)
            if flags & FLAG_WINDOW or op in (OP_END, OP_ERR):
                return op, flags, mid, b"".join(parts)

    def _ack(self, mid: int) -> None:
        self._queue = 0
        self._send([self._frame(OP_ACK, FLAG_WINDOW, mid, 0, b"")])

    def read_request(self) -> dict | None:
        chunks: list[bytes] = []
        flags = 0
        while True:
            try:
                op, flags, mid, body = self._read_window(self.reply_wait if chunks else None)
            except CoderError:
                if chunks:
                    raise
                return None
            if flags & FLAG_ALONE:
                continue
            chunks.append(body)
            if op == OP_PIECE:
                self._ack(mid)
                continue
            blob = b"".join(chunks)
            if op == OP_ERR:
                raise CoderError(blob.decode("utf-8", "replace"))
            if flags & FLAG_ZLIB and blob:
                blob = zlib.decompress(blob)
            return json.loads(blob.decode("utf-8"))

    def read_reply(self):
        """Куски ответа: пары (kind, text). Конец сообщения куском не является."""
        while True:
            op, flags, mid, body = self._read_window(self.reply_wait)
            if op == OP_ERR:
                raise CoderError(body.decode("utf-8", "replace"))
            if flags & FLAG_ZLIB and body:
                body = zlib.decompress(body)
            text = ""
            kind = "text"
            if body:
                obj = json.loads(body.decode("utf-8"))
                kind = obj.get("k") or "text"
                text = obj.get("s") or ""
            if op == OP_PIECE:
                if text:
                    yield kind, text
                self._ack(mid)
                continue
            if text:
                yield kind, text
            return


def messages_of(obj: dict) -> list[dict]:
    messages = list(obj.get("messages") or [])
    system = obj.get("system") or ""
    if system:
        messages = [{"role": "system", "content": system}] + messages
    return messages


def iter_llama(upstream: str, model: str, messages: list[dict], timeout: float):
    """Поток кусков (kind, text) с локального OpenAI-совместимого сервера."""
    url = upstream.rstrip("/") + "/chat/completions"
    payload = {"messages": messages, "stream": True, "temperature": 0.2}
    if model:
        payload["model"] = model
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as exc:
        raise CoderError(f"локальная модель не ответила: {exc}") from exc
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            blob = line[5:].strip()
            if blob == "[DONE]":
                return
            try:
                obj = json.loads(blob)
            except json.JSONDecodeError:
                continue
            delta = ((obj.get("choices") or [{}])[0] or {}).get("delta") or {}
            content = delta.get("content")
            if content:
                yield "text", content
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                yield "reasoning", reasoning


def serve(link: Link, complete, once: bool = False) -> None:
    """Принять запрос, отдать текст complete() снимками."""
    while True:
        req = link.read_request()
        if req is None:
            continue
        try:
            pending: list[str] = []
            kind_now = "text"
            last = time.monotonic()
            # Пока модель считает запрос, ответа ещё нет. Без кадра гость
            # через reply_wait решает, что окно не доехало.
            stop_beat = threading.Event()

            def beat() -> None:
                while not stop_beat.wait(10):
                    try:
                        link.transport.send(framing.build_frame(config.PING, 0, b""))
                    except Exception:
                        return

            beater = threading.Thread(target=beat, name="rdpconn-beat", daemon=True)
            beater.start()

            def flush(final: bool) -> None:
                nonlocal pending, last
                if not stop_beat.is_set():
                    stop_beat.set()
                    beater.join(timeout=3)
                text = "".join(pending)
                pending = []
                last = time.monotonic()
                if not text and not final:
                    return
                link.write_reply(kind_now, text, final)

            try:
                for kind, text in complete(messages_of(req)):
                    if kind != kind_now and pending:
                        flush(False)
                    kind_now = kind
                    pending.append(text)
                    blob = zlib.compress("".join(pending).encode("utf-8"))
                    if len(blob) >= (link.cells * CHUNK) // 2 or time.monotonic() - last >= 0.35:
                        flush(False)
                flush(True)
            finally:
                stop_beat.set()
        except Exception as exc:
            app.warn(f"!! кодер: {exc}")
            try:
                link.write_error(str(exc))
            except Exception as second:
                app.warn(f"!! ошибку не доставили: {second}")
        if once:
            return


def ask(link: Link, obj: dict):
    link.write_request(obj)
    yield from link.read_reply()


def _emit(kind: str, text: str) -> None:
    if kind == "reasoning":
        line = {"reasoning": text}
    else:
        line = {"text": text}
    sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def guest_loop(link: Link, once_path: str | None) -> int:
    from .statcell import ERR_CODER, ERR_JSON, FEED_MODEL, attach_guest

    board = attach_guest(link.transport, FEED_MODEL)
    app.warn("кодер в сессии: текст в буфер, порт не слушаю")

    def run(obj: dict) -> None:
        try:
            for kind, text in ask(link, obj):
                _emit(kind, text)
            sys.stdout.write('{"done":true}\n')
            sys.stdout.flush()
        except CoderError as exc:
            board.fail(ERR_CODER)
            sys.stdout.write(json.dumps({"error": str(exc)}, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    if once_path:
        run(json.loads(Path(once_path).read_text(encoding="utf-8")))
        return 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            board.fail(ERR_JSON)
            sys.stdout.write(json.dumps({"error": f"не json: {exc}"}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        run(obj)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rdpconn.coder",
        description="Текст локальной модели через буфер. host — рядом с моделью, guest — в сессии.",
    )
    p.add_argument("mode", choices=("host", "guest"))
    app.add_channel_args(p)
    p.add_argument("--upstream", default="http://127.0.0.1:8080/v1",
                   help="OpenAI-совместимый адрес модели на этой машине")
    p.add_argument("--model", default="",
                   help="имя модели в запросе; пусто — как решит сервер")
    p.add_argument("--once", default="",
                   help="guest: один запрос из JSON-файла и выход")
    p.add_argument("--cells", type=int, default=CELLS,
                   help=f"ячеек в одном снимке (по умолчанию {CELLS})")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        channel = app.open_channel(args)
    except app.UsageError as exc:
        app.warn(str(exc))
        return 2
    talk = app.warn if args.mode == "guest" else app.say
    talk(f"канал: {channel.label}")
    app.peer_hint(args)
    wait = args.wait
    store = getattr(channel.transport, "_store", None)
    if store is not None and store.__class__.__name__ == "WindowsClipboard" and wait < 12:
        wait = 12
        talk(".. жду снимок до 12 с")
    link = Link(channel.transport, cells=max(1, args.cells), reply_wait=wait)
    try:
        if args.mode == "host":
            app.say(f"кодер здесь, модель {args.upstream}")

            def complete(messages):
                yield from iter_llama(args.upstream, args.model, messages, timeout=max(wait, 30))

            serve(link, complete)
        else:
            return guest_loop(link, args.once or None)
    except KeyboardInterrupt:
        app.say("")
    finally:
        channel.close()
    return 0
