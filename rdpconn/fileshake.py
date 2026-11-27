"""Рукопожатие F1: координирует файловый канал (fileclip.py) с текстовым.

Оба канала делят один и тот же OS-буфер (docs/protocol.md 1 его не описывает —
как и C1/G1/S1, это не ошибка кода, спека до файлового слоя не дошла), но
ничего о существовании друг друга не знают. Если во время файловой передачи
где-то крутится запись текстового канала (heartbeat, обычный обмен), она
`OleSetClipboard`-ом сносит delay-render объект файла — передача обрывается
молча. F1 закрывает только межмашинную часть проблемы: сказать соседу "жду
файл, не пиши" и получить подтверждение, прежде чем класть файл в буфер.
Локальная часть (не писать в буфер самому, пока идёт передача) — забота
вызывающего кода, не этого модуля: он может приостановить свой же heartbeat
обычным вызовом метода, без сообщения по проводу.

Три сообщения, одним кадром NOTE каждое (без сегментации — список файлов
мелкий, многостраничные передачи сюда не укладываются и не нужны):

  FILE_OFFER  отправитель -> получатель: имена+размеры, предложенный чанк,
              оценка времени по известному потолку канала.
  FILE_READY  получатель -> отправитель: "жду файл, можно класть в буфер".
              Отправитель кладёт файл в буфер только после этого ACK — до
              него в буфере ничего нет, коллизий не бывает в принципе.
  FILE_DONE / FILE_FAIL
              получатель -> отправитель, после приёма: байты и sha256 по
              каждому файлу либо причина отказа. Доставка этого сообщения
              через текстовый канал и есть возврат к обычному режиму —
              отдельного сигнала "можно снова писать" не нужно.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import app, config, fileclip, framing

MAGIC = b"F1"
OP_OFFER = 1
OP_READY = 2
OP_DONE = 3
OP_FAIL = 4
# Кадр несёт 4 случайных байта, которые ни на что не влияют в разборе —
# только делают байты кадра разными между независимыми запусками. Без них
# повторный `send` того же файла тем же чанком после неудачной попытки
# публикует побайтово идентичный кадр (тот же magic+op+JSON), а дедупликация
# ячеек в clipboard.py (`_seen_frames`, см. её комментарий) узнаёт в нём уже
# виденную ячейку от прошлого процесса и тихо не отдаёт его наверх — не баг
# дедупликации (она там и для того, чтобы не путать повтор с новым кадром),
# а то, что F1 без nonce не давал ей отличить "то же самое" от "опять то же
# самое, но на самом деле другая попытка".
_NONCE_LEN = 4
_HEAD = 2 + 1 + _NONCE_LEN  # magic(2) + op(1) + nonce(4)

# Та же оценка потолка канала, что в памяти проекта (чанк 512 КБ-1 МБ, живой
# стенд F:\rdp-lab) — только для оценки времени ожидания FILE_DONE, не для
# тюнинга самой передачи.
ASSUMED_BPS = 5_000_000

# Минимум паузы ЧТЕНИЯ у отправителя после публикации файла (см.
# transport.suspend_reads() в send_files()) — не меньше документированного
# лага RDP-редиректа (10-15 с, docs/protocol.md 5, тот же порог, что уже
# принят для --ping x3): опрос буфера в ожидании FILE_DONE прерывается не
# потому, что файл большой, а потому, что собеседник может быть ещё за
# редиректом — свой же классический GetClipboardData в это время способен
# сорвать ещё не отданный FileContents (HRESULT 0x80040064, живой фикс
# 27.11.2026), даже у маленького файла с estimate_s≈0.
_READ_SUSPEND_FLOOR_S = 10.0


class HandshakeError(Exception):
    """Рукопожатие не состоялось: таймаут или явный отказ собеседника."""


def encode(op: int, body: bytes, nonce: bytes) -> bytes:
    if len(nonce) != _NONCE_LEN:
        raise ValueError(f"nonce должен быть {_NONCE_LEN} байт, получено {len(nonce)}")
    return MAGIC + bytes((op & 0xFF,)) + nonce + body


def decode(payload: bytes):
    """(op, nonce, body) — nonce отдельно, не слит в body: wait_result()/offer()
    сверяют его с тем, что сами отправили, прежде чем доверять кадру."""
    if len(payload) < _HEAD or payload[:2] != MAGIC:
        return None
    return payload[2], payload[3:_HEAD], payload[_HEAD:]


def _note(raw: bytes):
    """Кадр из транспорта — байты. Чужой тип и чужая магия просто не наши."""
    try:
        frame = framing.parse_frame(raw)
    except framing.FrameError:
        return None
    if frame.type != config.NOTE:
        return None
    return decode(frame.payload)


@dataclass
class Offer:
    files: list[tuple[str, int]]
    chunk: int
    estimate_s: float
    # Nonce кадра FILE_OFFER, из которого собран этот Offer — тот же, что
    # получатель обязан эхом вернуть в FILE_READY/FILE_DONE/FILE_FAIL (см.
    # wait_offer()), чтобы отправитель мог отличить ответ на ЭТОТ offer от
    # случайного старого кадра той же пары op в истории слота.
    nonce: bytes = b""

    @property
    def total_bytes(self) -> int:
        return sum(size for _, size in self.files)


class Handshake:
    """Один конец F1 поверх уже открытого транспорта (того же, что tx/rx/gate/coder)."""

    def __init__(self, transport) -> None:
        self.transport = transport
        self.seq = app.SeqCounter()
        # Свой на весь срок жизни объекта — см. комментарий у _NONCE_LEN:
        # один и тот же `Handshake` внутри одной попытки пускай хоть
        # повторяет кадры (это и есть дедупликация, как ей и положено
        # работать), а вот новый процесс с нуля обязан выглядеть по-новому.
        self._nonce = os.urandom(_NONCE_LEN)

    def _frame(self, op: int, body: bytes = b"") -> bytes:
        return framing.build_frame(config.NOTE, self.seq.take(), encode(op, body, self._nonce))

    def _send(self, op: int, body: bytes = b"") -> str | None:
        return app.send_frame(self.transport, self._frame(op, body))

    # --- отправитель файлов ---------------------------------------------

    def offer(self, paths: list[str | Path], *, chunk: int = 524288,
             ready_timeout: float = 20.0) -> Offer:
        """Предложить файлы, дождаться FILE_READY. Буфер ещё не тронут.

        ready_timeout по умолчанию покрывает лаг RDP-редиректа (10-15 с на
        живом стенде, docs/protocol.md 5) с запасом — короче не имеет смысла
        по тем же причинам, что и MIN_SILENCE_S у heartbeat.
        """
        entries = [(Path(p).name, os.path.getsize(p)) for p in paths]
        if not entries:
            raise HandshakeError("offer() без файлов")
        offer = Offer(files=entries, chunk=chunk,
                      estimate_s=round(sum(s for _, s in entries) / ASSUMED_BPS, 2),
                      nonce=self._nonce)
        body = json.dumps({"f": entries, "c": chunk, "e": offer.estimate_s},
                          ensure_ascii=False).encode("utf-8")
        if len(body) > config.MAX_PAYLOAD - _HEAD:
            raise HandshakeError(
                f"FILE_OFFER {len(body)} байт не влезает в один кадр "
                f"(предел {config.MAX_PAYLOAD - _HEAD}) — слишком много файлов "
                f"или длинные имена; сегментация не реализована")
        fail = self._send(OP_OFFER, body)
        if fail is not None:
            raise HandshakeError(f"FILE_OFFER не ушёл: {fail}")
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            raw = self.transport.receive(max(0.0, deadline - time.monotonic()))
            if raw is None:
                break
            parsed = _note(raw)
            if parsed is None:
                continue
            op, nonce, payload = parsed
            if nonce != self._nonce:
                # Чужой или устаревший обмен (тот же op, но не наш FILE_OFFER) —
                # лежит в истории слота от прошлой попытки/процесса; не наш ответ.
                continue
            if op == OP_READY:
                return offer
            if op == OP_FAIL:
                raise HandshakeError(
                    f"собеседник отказал: {payload.decode('utf-8', 'replace')}")
        raise HandshakeError("FILE_READY не дождались")

    def wait_result(self, timeout: float) -> list[tuple[str, int, str]]:
        """После send_files() — дождаться FILE_DONE. Бросает HandshakeError на FAIL/таймаут.

        nonce сверяется с self._nonce (тем же, что ушёл в FILE_OFFER): без
        этого первый же OP_DONE/OP_FAIL в очереди receive() принимался за
        ответ на ТЕКУЩУЮ передачу, даже если это случайный старый кадр той
        же пары op, оставшийся в истории слота от не связанной попытки —
        живой случай 27.11.2026, см. память проекта: host считал файл
        доставленным по чужому `{"f": []}` с прошлой попытки, пока настоящий
        FILE_FAIL этой самой передачи лежал в очереди следом и не был
        прочитан.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self.transport.receive(max(0.0, deadline - time.monotonic()))
            if raw is None:
                break
            parsed = _note(raw)
            if parsed is None:
                continue
            op, nonce, body = parsed
            if nonce != self._nonce:
                continue
            if op == OP_DONE:
                return [tuple(e) for e in json.loads(body.decode("utf-8"))["f"]]
            if op == OP_FAIL:
                raise HandshakeError(
                    f"получатель сообщил об ошибке: {body.decode('utf-8', 'replace')}")
        raise HandshakeError("FILE_DONE не дождались")

    # --- получатель файлов -----------------------------------------------

    def wait_offer(self, timeout: float | None = None) -> Offer | None:
        """Ждать FILE_OFFER. None — не дождались за timeout (None — ждать, пока не придёт).

        Принимает первый попавшийся OP_OFFER вне зависимости от nonce —
        получатель ещё не знает, какой обмен "свой", он как раз начинается
        этим кадром. С этого момента self._nonce принимает значение кадра
        (не своё, сгенерированное в __init__): accept()/decline()/
        report_done()/report_fail() дальше эхо'ят именно его, чтобы
        отправитель в offer()/wait_result() мог отличить ответ на ЭТОТ
        offer от случайного кадра той же пары op из прошлой попытки.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
            else:
                left = 5.0
            raw = self.transport.receive(left)
            if raw is None:
                continue
            parsed = _note(raw)
            if parsed is None:
                continue
            op, nonce, body = parsed
            if op != OP_OFFER:
                continue
            self._nonce = nonce
            obj = json.loads(body.decode("utf-8"))
            return Offer(files=[tuple(e) for e in obj["f"]], chunk=obj["c"], estimate_s=obj["e"],
                        nonce=nonce)

    def accept(self) -> None:
        fail = self._send(OP_READY)
        if fail is not None:
            raise HandshakeError(f"FILE_READY не ушёл: {fail}")

    def decline(self, reason: str) -> None:
        # Best-effort: не дойдёт — отправитель и так увидит отказ по таймауту ready_timeout.
        self._send(OP_FAIL, reason.encode("utf-8", "replace")[:config.MAX_PAYLOAD - _HEAD])

    def report_done(self, results: list[tuple[str, int, str]]) -> None:
        body = json.dumps({"f": results}, ensure_ascii=False).encode("utf-8")
        if len(body) > config.MAX_PAYLOAD - _HEAD:
            # Отчёт не влез (длинные имена/много файлов) — отправитель должен
            # узнать хотя бы, что что-то не так, а не ждать молча до таймаута.
            self.report_fail(f"{len(results)} файлов принято, отчёт не влез в кадр")
            return
        fail = self._send(OP_DONE, body)
        if fail is not None:
            app.warn(f"?? FILE_DONE не ушёл: {fail}")

    def report_fail(self, reason: str) -> None:
        self._send(OP_FAIL, reason.encode("utf-8", "replace")[:config.MAX_PAYLOAD - _HEAD])


# --- высокоуровневая оркестровка: рукопожатие + сам fileclip -----------------


def send_files(transport, paths: list[str | Path], *, chunk: int = 524288,
               ready_timeout: float = 20.0,
               result_timeout: float | None = None,
               pause: bool = False) -> list[tuple[str, int, str]]:
    """Полный цикл отправителя: offer -> ready -> send_files() -> done.

    result_timeout по умолчанию — оценка времени передачи (Offer.estimate_s)
    с трёхкратным запасом плюс минимум 30 с: короткие файлы доезжают почти
    мгновенно, а запас нужен не на сами байты, а на сумму ретраев получателя
    (list_clipboard_files — до 30×0.15 с, recv_file — до 15×0.1 с, плюс лаг
    редиректа туда и обратно) — живьём эта сумма спокойно съедала прежний
    запас в 15 с, хотя сама передача укладывалась в секунды.

    pause=True — поставить ClipboardTransport.pause() вокруг fileclip.send_files()
    и wait_result(): offer() пишет в тот же транспорт (FILE_OFFER) и поэтому
    остаётся снаружи паузы — поставить его под паузу значило бы, что
    собственный FILE_OFFER дожидается самого себя. Нужно только там, где
    транспорт делят с чужим фоновым писателем в этом же процессе (DropWatcher
    в ends.py); самостоятельный `python -m rdpconn.fileshake send` им не
    делится ни с кем — но suspend_reads() ниже применяется всегда, не только
    при pause=True: это не про дележ транспорта с соседом, а про то, что
    собственный receive() и в одиночку способен сорвать свою же передачу.

    27.11.2026, два фикса подряд на одном и том же HRESULT 0x80040064 — для
    памяти, чтобы не повторить по кругу:
    1) `resume()` переставлен ПОСЛЕ wait_result() вместо сразу после
       send_files() — не хватило: `_reconcile()`'s один классический
       GetClipboardData был не единственным источником гонки.
    2) Настоящая причина шире: `receive()` по конструкции читает буфер
       классическим API даже во время паузы (пауза блокирует только запись,
       см. докстринг `pause()`) — а wait_result() обязан звать receive()
       в цикле, пока не придёт FILE_DONE/FAIL, то есть всё это время
       (минимум result_timeout) собственный опрос конкурирует с ещё не
       отданным FileContents того же буфера. Живой фикс — `suspend_reads()`
       (clipboard.py): отдельное, более сильное окно, где receive() не
       трогает буфер вовсе, не только не пишет.
    """
    hs = Handshake(transport)
    offer = hs.offer(paths, chunk=chunk, ready_timeout=ready_timeout)
    if result_timeout is None:
        result_timeout = max(30.0, offer.estimate_s * 3 + 20.0)
    # FILE_READY только что доехал текстовым каналом (CF_UNICODETEXT) — он же
    # SetClipboardData, а не OleSetClipboard, который вот-вот вызовет
    # fileclip.send_files(). Без паузы иногда виден тот же класс гонки, что уже
    # пойман внутри fileclip.py (редиректор публикует формат раньше, чем
    # честно на него отвечает): OleGetClipboard/GetData на стороне получателя
    # падает несколько раз подряд, пока retry внутри fileclip не выберет всю
    # свою квоту. Небольшая пауза здесь — не замена тем retry (они остаются),
    # а снижение частоты, с которой их квота вообще нужна.
    time.sleep(0.5)
    if pause:
        transport.pause()
    try:
        fileclip.send_files(paths)
        # Не меньше _READ_SUSPEND_FLOOR_S даже для крошечного файла (лаг
        # редиректа и ретраи получателя не зависят от размера), не больше
        # result_timeout минус запас на сам финальный опрос FILE_DONE/FAIL.
        suspend_s = min(max(0.0, result_timeout - 2.0),
                        max(_READ_SUSPEND_FLOOR_S, offer.estimate_s * 3))
        transport.suspend_reads(suspend_s)
        return hs.wait_result(result_timeout)
    finally:
        if pause:
            transport.resume()


def recv_files(transport, out_dir: str | Path, *, offer_timeout: float | None = None,
              chunk_override: int | None = None,
              file_timeout_margin: float = 30.0,
              pause: bool = False) -> list[tuple[str, int, str]] | None:
    """Полный цикл получателя: ждать offer -> ready -> recv -> done/fail.

    None — offer_timeout истёк, никто ничего не предлагал (не ошибка, а
    пустой результат: вызывающий решает, повторить ожидание или нет).
    pause — см. send_files(), тот же смысл для приёмной стороны.
    """
    hs = Handshake(transport)
    offer = hs.wait_offer(offer_timeout)
    if offer is None:
        return None
    hs.accept()
    return _receive_body(hs, transport, offer, out_dir, chunk_override=chunk_override,
                         file_timeout_margin=file_timeout_margin, pause=pause)


def _receive_body(hs: Handshake, transport, offer: Offer, out_dir: str | Path, *,
                  chunk_override: int | None = None, file_timeout_margin: float = 30.0,
                  pause: bool = False) -> list[tuple[str, int, str]]:
    """Общий хвост recv_files() после accept(): скачать, посчитать sha256, отчитаться.

    Вынесено отдельно, чтобы DropWatcher мог войти сюда уже имея решённый
    offer (из кадра, перехваченного на чужом цикле получения) — заново гонять
    wait_offer()/accept() там не на чём, кадр уже прочитан.
    """
    chunk = chunk_override or offer.chunk
    seq_before = fileclip.sequence_number()
    out_dir = str(out_dir)
    try:
        if pause:
            transport.pause()
        try:
            fileclip.wait_for_new_file(seq_before, timeout=offer.estimate_s * 3 + file_timeout_margin)
            entries = fileclip.list_clipboard_files(formatetc_retries=30, formatetc_retry_wait=0.15)
            os.makedirs(out_dir, exist_ok=True)
            results: list[tuple[str, int, str]] = []
            for i, entry in enumerate(entries):
                out_path = os.path.join(out_dir, entry.name)
                recv = fileclip.recv_file(i, out_path, chunk=chunk)
                digest = hashlib.sha256(Path(out_path).read_bytes()).hexdigest()
                results.append((entry.name, recv.bytes_total, digest))
        finally:
            if pause:
                transport.resume()
    except Exception as exc:
        hs.report_fail(str(exc))
        raise
    hs.report_done(results)
    return results


class DropWatcher:
    """F1 поверх занятого транспорта: обслуживает папку, пока host/guest
    (ends.py) заняты обычным текстовым обменом.

    Второй процесс рядом с host/guest делить тот же слот не может (см. правило
    «один читатель на сторону», AGENTS.md 3) — значит, файловый обмен обязан
    жить в том же процессе, тем же транспортом. tick() — единственная точка
    входа: вызывающий код сам решает, когда момент простоя достаточно безопасен
    (не середина разбора запроса/ответа), и передаёт сюда либо чужой кадр, на
    который не отозвался его собственный протокол (raw), либо None — опрос по
    расписанию без нового кадра. Ничего из tick() не просачивается наружу как
    исключение: одна неудачная файловая передача не должна валить канал модели/сети.
    """

    def __init__(self, transport, drop_dir: str | Path, *, chunk: int = 524288,
                outbox_retry_s: float = 60.0) -> None:
        self.transport = transport
        drop = Path(drop_dir)
        self.inbox = drop / "in"
        self.outbox = drop / "out"
        self.sent = self.outbox / "sent"
        self.chunk = chunk
        self.outbox_retry_s = outbox_retry_s
        self._outbox_fail_until = 0.0

    def tick(self, raw: bytes | None) -> None:
        try:
            if raw is not None:
                self._handle_incoming(raw)
            self._check_outbox()
        except Exception as exc:
            app.warn(f"!! fileshake (drop): {exc}")

    def _handle_incoming(self, raw: bytes) -> None:
        parsed = _note(raw)
        if parsed is None or parsed[0] != OP_OFFER:
            return
        _op, nonce, body = parsed
        obj = json.loads(body.decode("utf-8"))
        offer = Offer(files=[tuple(e) for e in obj["f"]], chunk=obj["c"], estimate_s=obj["e"],
                     nonce=nonce)
        hs = Handshake(self.transport)
        # Эхо nonce'а из offer, не свой свежий: offer()/wait_result() на
        # стороне отправителя сверяют его с тем, что сами послали в
        # FILE_OFFER — своим нонсом здесь FILE_READY/FILE_DONE выглядел бы
        # для них чужим кадром и был бы молча пропущен (см. docstring
        # wait_offer()).
        hs._nonce = nonce
        hs.accept()
        self.inbox.mkdir(parents=True, exist_ok=True)
        try:
            results = _receive_body(hs, self.transport, offer, str(self.inbox), pause=True)
        except Exception as exc:
            app.warn(f"!! приём файла (drop) не удался: {exc}")
            return
        for name, n, digest in results:
            app.say(f".. принят файл (drop): {name} {n} байт sha256={digest}")

    def _check_outbox(self) -> None:
        if time.monotonic() < self._outbox_fail_until:
            return
        try:
            paths = sorted(p for p in self.outbox.iterdir() if p.is_file())
        except FileNotFoundError:
            return
        if not paths:
            return
        try:
            results = send_files(self.transport, paths, chunk=self.chunk, pause=True)
        except HandshakeError as exc:
            # Без отступа повтор каждые несколько секунд долбил бы собеседника
            # тем же отказом (занят, таймаут) до бесконечности.
            self._outbox_fail_until = time.monotonic() + self.outbox_retry_s
            app.warn(f"!! отправка файла (drop) не удалась: {exc}")
            return
        self.sent.mkdir(parents=True, exist_ok=True)
        for path in paths:
            path.replace(self.sent / path.name)
        for name, n, digest in results:
            app.say(f".. файл отправлен (drop): {name} {n} байт sha256={digest}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rdpconn.fileshake",
        description="Файл(ы) через буфер с подтверждением по текстовому каналу (F1).",
    )
    sub = p.add_subparsers(dest="mode", required=True)

    p_send = sub.add_parser("send", help="предложить и передать файл(ы) с подтверждением")
    p_send.add_argument("paths", nargs="+")
    p_send.add_argument("--chunk", type=int, default=524288)
    p_send.add_argument("--ready-timeout", type=float, default=20.0)
    p_send.add_argument("--result-timeout", type=float, default=None,
                        help="сколько ждать FILE_DONE; пусто — оценка по размеру с запасом")
    app.add_channel_args(p_send)

    p_recv = sub.add_parser("recv", help="ждать предложение и принять файл(ы) с подтверждением")
    p_recv.add_argument("out_dir")
    p_recv.add_argument("--offer-timeout", type=float, default=None,
                        help="сколько ждать FILE_OFFER; пусто — ждать, пока не придёт")
    app.add_channel_args(p_recv)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        channel = app.open_channel(args, role=f"fileshake-{args.mode}")
    except app.UsageError as exc:
        app.warn(str(exc))
        return 2
    app.say(f"канал: {channel.label}")
    app.peer_hint(args)
    try:
        if args.mode == "send":
            results = send_files(channel.transport, args.paths, chunk=args.chunk,
                                 ready_timeout=args.ready_timeout,
                                 result_timeout=args.result_timeout)
            for name, n, digest in results:
                app.say(f"принято собеседником: {name} {n} байт sha256={digest}")
        else:
            results = recv_files(channel.transport, args.out_dir,
                                 offer_timeout=args.offer_timeout)
            if results is None:
                app.warn("предложения не дождались")
                return 1
            for name, n, digest in results:
                app.say(f"[{name}] {n} байт sha256={digest}")
    except HandshakeError as exc:
        app.warn(f"!! {exc}")
        return 1
    finally:
        channel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
