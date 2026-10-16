"""Общая часть двух программ: открытие канала, аргументы, обмен, heartbeat, вывод.

tx и rx различаются только циклом, поэтому всё остальное здесь. Канал один —
буфер обмена (rdpconn.clipboard); он выбран не как замена звуку, а потому что
в RDP-среде, где канал проверялся, не работает ни одно плечо звука, а сеть
оставляет только порт 3389, то есть сам RDP. Буфер обмена живёт внутри того же
соединения: порты, брандмауэр и права администратора не нужны.

Обмен — честный полудуплекс (docs/protocol.md 4): отправитель шлёт CMD и ждёт
ACK с тем же seq, приёмник разбирает кадр и отвечает ACK. Кадр с испорченной
CRC даёт FrameError — на него положен NAK, и он тоже отправляется, иначе
испорченный кадр неотличим от отсутствия сигнала.
"""

from __future__ import annotations

import argparse
import sys
import time

from . import config, framing
from .framing import Frame, FrameError


class UsageError(Exception):
    """Неверный запуск. str() — текст для stderr, код возврата 2."""


# --- аргументы ---------------------------------------------------------------


def add_channel_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--slot", default="win",
                   help="слот буфера: win | memory | file:PATH (для проверки без Windows)")
    p.add_argument("--peer-id", default=None,
                   help="метка этого конца в слоте; по умолчанию случайная, "
                        "поэтому для боя её задают явно (A и B)")
    p.add_argument("--hotkey", default=None,
                   help="клавиша паузы обмена, например ctrl+alt+f9 (по умолчанию выключена)")
    p.add_argument("--pause-timeout", type=float, default=60.0,
                   help="автовозобновление после N с паузы; 0 — ждать вечно")
    p.add_argument("--restore", action="store_true",
                   help="по выходе из паузы вернуть в слот последний наш кадр")
    p.add_argument("--no-auto-pause", dest="auto_pause", action="store_false",
                   help="не вставать на паузу, когда в слот попала чужая копия")
    p.add_argument("--ping", type=float, default=PING_INTERVAL_S,
                   help=f"интервал heartbeat в секундах, 0 — выключить "
                        f"(по умолчанию {PING_INTERVAL_S:g})")
    p.add_argument("--codepage", default=config.CODEPAGE,
                   help=f"кодовая страница текста (по умолчанию {config.CODEPAGE})")
    p.add_argument("--wait", type=float, default=CLIPBOARD_WAIT_S,
                   help=f"сколько секунд ждать ответа (по умолчанию "
                        f"{CLIPBOARD_WAIT_S:g})")


class Channel:
    """Канал: транспорт плюс то немногое, что у него спрашивают tx/rx."""

    def __init__(self, transport) -> None:
        self.transport = transport
        self.label = transport.describe

    def close(self) -> None:
        closer = getattr(self.transport, "close", None)
        if closer is not None:
            closer()


def _pause_reporter(hotkey, pause_timeout):
    """Коллбэк смены состояния слота.

    Состояния сообщают о разном, и путать их нельзя.

    paused (ручная пауза клавишей) снимает обмен с очереди полностью, пока
    человек не нажмёт клавишу снова, — про неё надо сказать способ возврата.
    busy (в слоте человеческая копия) ничего не останавливает: это заметка
    «я вижу, что буфер твой», а не предупреждение о поломке. Писать про неё
    тем же голосом, что про обрыв, значит пугать людей понапрасну и
    выдрессировать их игнорировать настоящие предупреждения.
    """
    def report(state: str) -> None:
        if state == "resumed":
            say("<< обмен продолжен")
        elif state == "paused":
            how = (f"нажмите {hotkey}, чтобы продолжить" if hotkey
                   else (f"вернётся само через {pause_timeout:.0f} с"
                         if pause_timeout else "продолжить нечем: задайте --hotkey"))
            warn(f"!! обмен на паузе: буфер отдан вам — {how}")
        elif state == "busy":
            say(".. в слоте чужая копия (обмен работает, слот не мой)")

    return report


def open_channel(args: argparse.Namespace) -> Channel:
    """Собрать канал по аргументам."""
    from .clipboard import ClipboardError, ClipboardTransport, open_store

    try:
        store = open_store(args.slot)
        transport = ClipboardTransport(
            store, peer_id=args.peer_id, hotkey=args.hotkey,
            auto_pause=args.auto_pause, pause_timeout=args.pause_timeout,
            restore=args.restore,
            on_pause=_pause_reporter(args.hotkey, args.pause_timeout),
        )
    except ClipboardError as exc:
        raise UsageError(str(exc)) from None
    return Channel(transport)


def peer_hint(args: argparse.Namespace) -> None:
    """Предупредить о случайном метке конца.

    Без --peer-id метка случайная, и это не косметика: при каждом перезапуске
    сторона выступает под новым id, её старые ячейки остаются в слоте чужим
    наследием, а собеседник лишается возможности сопоставить кадры одного конца
    между двумя запусками. Фиксированная метка делает рестарт собеседника
    рядовым событием — ровно тем, ради чего существует reset_seen.
    """
    if args.peer_id:
        return
    warn(".. метка конца случайная: задайте --peer-id (A и B), иначе после "
         "перезапуска вы станете для собеседника новым концом")


# Сколько ждать ответ. T_CARRIER звукового канала (0.5 с) здесь не годится: он
# рассчитан на непрерывный тон, а в буфере ответ — это запись, которая
# появляется, только когда другой процесс успел её сделать. 1 с покрывает опрос
# + запись ACK + повторный опрос без лишней задержки.
CLIPBOARD_WAIT_S = 1.0


def wait_timeout(args: argparse.Namespace) -> float:
    return args.wait


# --- обмен -------------------------------------------------------------------


class SeqCounter:
    """Порядковые номера исходящих кадров: 0..255 по кругу (docs/protocol.md 4)."""

    def __init__(self, start: int = 0) -> None:
        self._next = start & 0xFF

    def take(self) -> int:
        seq = self._next
        self._next = (seq + 1) & 0xFF
        return seq


def send_frame(transport, frame_bytes: bytes):
    """Отправить кадр. None — успех, иначе текст причины отказа.

    Буферный канал может отказать, когда слот отдан человеку (ручная пауза): он
    ждёт освобождения и через таймаут сдаётся. Ронять этим программу нельзя —
    приёмник с активным Ctrl+Alt+F9 потерял бы цикл целиком.
    """
    from .clipboard import ClipboardError

    try:
        transport.send(frame_bytes)
        return None
    except ClipboardError as exc:
        return str(exc)


def send_command(transport, counter: SeqCounter, text: str,
                 seq: int | None = None, codec: str = config.CODEPAGE):
    """CMD с текстом команды.

    Возвращает (seq, None) при успехе или (seq, текст причины) при отказе —
    см. send_frame.

    seq передаётся при ретрансмиссии. Протокол (docs/protocol.md 4) требует
    повторять кадр с тем же seq, а не брать новый: повтор с новым номером
    неотличим от новой команды, и приёмник исполнит её второй раз.
    """
    if seq is None:
        seq = counter.take()
    return seq, send_frame(transport, framing.cmd(seq, text, codec))


def receive_frame(transport, timeout: float):
    """Дождаться и разобрать кадр.

    Возвращает (Frame | None, FrameError | None):
      (Frame, None)      — валидный кадр;
      (None, FrameError) — кадр принят, но испорчен (CRC/длина/тип) — на него NAK;
      (None, None)       — за timeout кадра не было.
    """
    raw = transport.receive(timeout)
    if raw is None:
        return None, None
    try:
        return framing.parse_frame(raw), None
    except FrameError as exc:
        return None, exc


# --- heartbeat ---------------------------------------------------------------

# Интервал по умолчанию. Взят с расчётом на буферный канал, где обмен идёт
# пачками и пауза в полминуты — норма, а не признак обрыва; при этом молчание
# дольше трёх интервалов уже означает, что собеседник молчит ~90 с.
PING_INTERVAL_S = 25.0
# Сколько интервалов молчания терпеть до объявления обрыва. Три, а не один:
# один потерянный PING (слот в этот момент держит человек) не должен объявлять
# канал мёртвым.
PING_MISSES = 3
# Порог, ниже которого объявление обрыва становится ложным. Редирект буфера в
# RDP запаздывает сам, и при коротком --ping живой канал начал бы выглядеть
# потерянным. Значение округлено вверх от наблюдаемого на живом стенде лага в
# 10–15 с: ниже него предупреждение перестаёт отличать обрыв от задержки.
MIN_SILENCE_S = 30.0


class Heartbeat:
    """Жив ли собеседник: PING по таймеру, PONG в ответ, счётчик молчания.

    Зачем это нужно именно здесь. Транспорт буфера умеет отказать в записи и
    умеет молча ничего не принять, но молчание приёмника неотличимо от его
    спокойствия: оба выглядят как отсутствие кадров. Без heartbeat tx стоит с
    пустым экраном, а человек гадает — канал живой или собеседник умер.

    Порог считается не по нашим PING, а по любому принятому кадру: живой
    собеседник, честно отвечающий NOTE, жив, гонять ему пинги незачем, а
    молчание после последней видимой от него активности и есть обрыв.

    proactive=False — сторона только отвечает на PING, но не заводит своих. Так
    работает приёмник: живой собеседник и так пингует, а ответный PONG — уже
    признак жизни, поэтому порог молчания отрабатывает без единого нашего
    контрольного кадра. Двусторонний пинг удваивает число записей в слот, а
    каждая запись в буферном канале — это ещё и событие RDP-редиректа; на
    медленном редиректе это превращает heartbeat в фоновый шум, который сам же
    и мешает обмену.
    """

    def __init__(self, interval: float = PING_INTERVAL_S,
                 misses: int = PING_MISSES, proactive: bool = True) -> None:
        self.interval = interval
        # 0 выключает heartbeat: канал нужен молча, без посторонних кадров.
        self.enabled = bool(interval) and interval > 0
        self.misses = misses
        self.proactive = proactive
        self.deadline = interval * misses if self.enabled else 0.0
        self._ping_seq = SeqCounter()
        now = time.monotonic()
        self._last_seen = now
        self._last_ping = now
        self.alive = True

    def note(self) -> bool:
        """Отметить признак жизни. True — собеседник считался потерянным.

        Отодвигается и время последнего PING, а не только момент приёма: живой
        обмен уже и есть доказательство живости, слать поверх него контрольные
        пинги — значит забивать слот кадрами, которых никто не ждал. Heartbeat
        нужен ровно тогда, когда разговора нет.
        """
        revived = not self.alive
        now = time.monotonic()
        self._last_seen = now
        self._last_ping = now
        self.alive = True
        return revived

    def due(self) -> bool:
        """Пора ли послать PING.

        Сторона без proactive своих пингов не шлёт вовсе: её дело отвечать, а
        молчание собеседника она заметит и так — по порогу ниже.

        Пока собеседник считается потерянным, пинги не шлём: PING в пустоту
        ничего не проверяет, а ждать его ответа — значит держать в транспорте
        лишние кадры для того, кого всё равно нет.
        """
        if not self.enabled or not self.alive or not self.proactive:
            return False
        return time.monotonic() - self._last_ping >= self.interval

    def ping(self) -> bytes:
        self._last_ping = time.monotonic()
        return framing.ping(self._ping_seq.take())

    def silence(self) -> float:
        """Сколько секунд от собеседника не было ни одного кадра."""
        return time.monotonic() - self._last_seen

    def expired(self) -> bool:
        """Молчание собеседника дольше порога — считать его потерянным."""
        return self.enabled and self.alive and self.silence() >= self.deadline

    def wait(self, timeout: float) -> float:
        """Сколько спать в receive, чтобы не задержать собственный PING.

        Просыпаться чаще, чем раз в интервал, необходимо и для детекта обрыва:
        порог в несколько интервалов иначе проверялся бы только тогда, когда
        кадры и так идут, то есть никогда.
        """
        if not self.enabled:
            return timeout
        until_ping = self.interval - (time.monotonic() - self._last_ping)
        return max(0.05, min(timeout, until_ping))

    def resync(self) -> None:
        """Начать отсчёт заново, оставив собеседника потерянным.

        Отсчёт сбрасывается, чтобы обрыв не объявлялся на каждом проходе цикла
        (иначе одно и то же сообщение сыпалось бы каждые POLL_S). Состояние
        alive при этом не восстанавливается: вернуть его может только реальный
        кадр — иначе «мёртвый» канал выглядел бы живым без единого признака.
        """
        now = time.monotonic()
        self._last_seen = now
        self._last_ping = now

    def mark_lost(self) -> None:
        """Собеседник потерян: ждать его кадр, своих пингов не слать."""
        self.alive = False
        self.resync()


def make_heartbeat(args: argparse.Namespace, proactive: bool = True) -> Heartbeat:
    """Собрать heartbeat и предупредить о заведомо ложном пороге.

    Порог обрыва — интервал × число пропусков, и на буферном канале он обязан
    превышать не только время ответа, но и задержку самого RDP-редиректа: запись
    в буфер появляется у собеседника не мгновенно. С --ping 5 порог выходит
    15 с, и живой канал объявляется потерянным примерно на каждом обмене —
    человек перестаёт верить предупреждениям, а верить им нужно ровно тогда,
    когда обрыв настоящий.
    """
    hb = Heartbeat(args.ping, proactive=proactive)
    if hb.enabled and hb.deadline < MIN_SILENCE_S:
        warn(f".. порог обрыва {hb.deadline:.0f} с мал для буферного канала: "
             f"редирект RDP сам по себе запаздывает, объявление о потере будет "
             f"ложным. Для прогона это нормально, для работы — нет")
    if not hb.proactive:
        say(".. контрольные PING не шлю: отвечаю на чужие, обрыв вижу по молчанию")
    return hb


def heartbeat_handle(hb, transport, frame) -> bool:
    """Обработать служебный кадр heartbeat. True — кадр поглощён.

    PING требует PONG с тем же seq: без ответа собеседник объявит нас
    потерянными, хотя мы живы и просто молчали.

    Возрождение печатается и здесь тоже: иначе сторона, объявившая обрыв,
    никогда не сказала бы, что канал вернулся. Обрыв объявляется в одном месте,
    а снимается в двух (обычный кадр и PONG), и молчание во втором случае
    оставляло бы человека с предупреждением, которое уже не в силе.
    """
    if frame is None or not hb.enabled:
        return False
    if frame.type == config.PING:
        if hb.note():
            say(".. собеседник снова отвечает")
        fail = send_frame(transport, framing.pong(frame.seq))
        if fail is None:
            say(f">> PONG seq={frame.seq}")
        else:
            warn(f"?? PONG seq={frame.seq} не отправлен: {fail}")
        return True
    if frame.type == config.PONG:
        if hb.note():
            say(".. собеседник снова отвечает")
        say(f"<< PONG seq={frame.seq}")
        return True
    return False


def heartbeat_tick(hb, transport) -> bool:
    """Послать PING, если пришло время. True — пинг ушёл."""
    if not hb.due():
        return False
    fail = send_frame(transport, hb.ping())
    if fail is None:
        say(">> PING")
        return True
    warn(f"?? PING не отправлен: {fail}")
    return False


def heartbeat_check(hb, transport) -> bool:
    """Проверить молчание собеседника. True — обрыв объявлен впервые."""
    if not hb.expired():
        return False
    warn(f"!! собеседник молчит {hb.silence():.1f} с — канал считается потерянным")
    hb.mark_lost()
    # Транспорт обязан забыть принятые кадры: за время молчания собеседник мог
    # перезапуститься и повести счётчик с нуля, и запомненные номера заставили
    # бы его первые кадры выглядеть повтором — обрыв сменился бы вечным
    # молчанием при формально живом канале.
    resync = getattr(transport, "reset_seen", None)
    if resync is not None:
        resync()
    return True


# --- вывод -------------------------------------------------------------------


def say(text: str) -> None:
    """Печать в кодировке консоли без UnicodeEncodeError."""
    _emit(sys.stdout, text)


def warn(text: str) -> None:
    _emit(sys.stderr, text)


def _emit(stream, text: str) -> None:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    data = (text + "\n").encode(encoding, errors="replace")
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
    else:
        stream.write(data.decode(encoding, errors="replace"))
        stream.flush()
