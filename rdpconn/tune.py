"""Замер паузы повтора снимка и запас на рабочую скорость канала.

Повтор новой ячейкой — то, что на редиректе отделяет дошедший снимок от
стёртого ответа. Предел ищется шагом 0.1 с: ниже него повтор уже ненадёжен.
В работу ставится предел × PACE_MARGIN. Быстрый первый уход записи пределом
не считается: он проходит мимо повтора.

Замер — это PING и PONG, не команда. Формат кадра не меняется.
"""

from __future__ import annotations

import time

from . import app, config, framing
from .clipboard import NUDGE_AFTER_S

# Ответ после повтора на стенде приходил примерно через 0.1 с сверх паузы.
_SLACK_S = 0.6
_pace: "Pace | None" = None


def operating(limit: float) -> float:
    """Пауза с запасом. 0.7 с → 0.94 с."""
    tenths = int(round(limit * 10))
    cents = (tenths * int(round(config.PACE_MARGIN * 100)) + 9) // 10
    return cents / 100


def judge(probe, delay: float, tries: int = config.PACE_TRIES) -> str:
    """«ok», если повтор на этой паузе подтверждён дважды.

    Один промах паузу не бракует: на стенде предел брали по двум удачам из трёх.
    Два промаха бракуют сразу. «fast» — запись ушла до повтора и предел не доказывает.
    """
    oks = 0
    fails = 0
    for _ in range(tries):
        result = probe(delay)
        if result == "ok":
            oks += 1
        elif result == "fail":
            fails += 1
        if oks >= 2:
            return "ok"
        if fails >= 2:
            return "fail"
    if oks >= 2:
        return "ok"
    if fails >= 2:
        return "fail"
    return "unproven"


def search(probe, start: float = 1.0) -> tuple[float | None, bool]:
    """Наименьшая пауза повтора и был ли вообще ответ.

    None при живом собеседнике значит, что запись уходила до повтора и
    предел повтора эти пробы не доказали. Паузу тогда не укорачивают.
    """
    peer = False

    def wrap(delay: float) -> str:
        nonlocal peer
        result = probe(delay)
        if result in ("ok", "fast"):
            peer = True
        return result

    step = config.PACE_STEP_S
    delay = round(start, 2)
    best = None
    first = None
    while delay >= config.PACE_FLOOR_S - 1e-9:
        status = judge(wrap, delay)
        if first is None:
            first = status
        if status != "ok":
            break
        best = delay
        delay = round(delay - step, 2)
    if best is not None:
        return best, True
    # Стартовая пауза ответила слишком быстро — повтор ею не проверен.
    # Удлинять её незачем: канал не медленнее, он просто не дошёл до повтора.
    if first != "fail":
        return None, peer
    delay = round(start + step, 2)
    while delay <= config.PACE_CEILING_S + 1e-9:
        if judge(wrap, delay) == "ok":
            return delay, True
        delay = round(delay + step, 2)
    return None, peer


def windows(transport) -> bool:
    store = getattr(transport, "_store", None)
    return store is not None and store.__class__.__name__ == "WindowsClipboard"


def use(pace: "Pace") -> None:
    global _pace
    _pace = pace


def active() -> "Pace | None":
    return _pace


def touch() -> None:
    """Команда, не heartbeat: от неё считается простой."""
    if _pace is not None:
        _pace.touch()


def on_exchange_failed() -> None:
    """Обмен без подтверждения. Следующая попытка идёт с более длинной паузой."""
    if _pace is not None:
        _pace.relax()


class Pace:
    """Держит измеренный предел и выставляет паузу повтора."""

    def __init__(self, idle_offset: float = 0.0) -> None:
        self.limit: float | None = None
        self.delay = NUDGE_AFTER_S
        self.dirty = False
        self._seqs = app.SeqCounter(200)
        now = time.monotonic()
        self._last_work = now
        self._last_seen = 0.0
        # Сдвиг, чтобы две стороны не писали проверку в один и тот же момент.
        self._next_idle = now + config.PACE_IDLE_S + idle_offset

    def touch(self) -> None:
        self._last_work = time.monotonic()

    def seen(self) -> None:
        self._last_seen = time.monotonic()

    def apply(self, delay: float) -> None:
        from . import clipboard
        clipboard.NUDGE_AFTER_S = delay
        self.delay = delay

    def relax(self) -> None:
        """Сдвинуть предел вверх на шаг и сразу удлинить паузу."""
        base = self.limit if self.limit is not None else round(self.delay / config.PACE_MARGIN, 2)
        self.limit = min(config.PACE_CEILING_S, round(base + config.PACE_STEP_S, 2))
        self.dirty = True
        self.apply(operating(self.limit))
        app.say(f".. ошибка обмена, повтор снимка {self.delay:.2f} с до новой проверки")

    def measure(self, transport, reason: str) -> bool:
        """Полный шаг вниз от текущей стартовой паузы. False — канал не ответил."""

        def probe(delay: float) -> str:
            return sample(transport, delay, self._seqs.take())

        try:
            limit, peer = search(probe)
        finally:
            drain(transport, 1.2)
        if limit is None and peer:
            self.apply(NUDGE_AFTER_S)
            self.dirty = False
            app.say(".. канал отвечает до повтора, пауза 1.00 с")
            return True
        if limit is None:
            self.apply(NUDGE_AFTER_S)
            app.warn(".. канал не ответил на проверку скорости, пауза без изменений")
            self.dirty = False
            return False
        self.limit = limit
        self.dirty = False
        self.apply(operating(limit))
        app.say(f".. канал: повтор снимка {limit:.2f} с, с запасом {self.delay:.2f} с ({reason})")
        return True

    def maybe_idle(self, transport) -> None:
        """На простое проверить, не стал ли канал быстрее или тише.

        Без недавнего кадра собеседника не пишем: проверка в пустую сессию
        затёрла бы человеческую копию в буфере.
        """
        now = time.monotonic()
        if now - self._last_work < config.PACE_IDLE_S or now < self._next_idle:
            return
        if self._last_seen == 0.0 or now - self._last_seen > 60.0:
            return
        if not _quiet(transport):
            self._next_idle = now + 30.0
            return
        self._next_idle = now + config.PACE_IDLE_S
        self.touch()
        if self.dirty or self.limit is None:
            self.measure(transport, "после ошибки" if self.dirty else "простой")
            return
        trial = round(self.limit - config.PACE_STEP_S, 2)
        if trial < config.PACE_FLOOR_S:
            return
        result = sample(transport, trial, self._seqs.take())
        if result != "ok":
            return
        # Один удачный шаг не перебивает предел: нужна вторая проба, как в поиске.
        if sample(transport, trial, self._seqs.take()) != "ok":
            return
        self.limit = trial
        self.apply(operating(trial))
        app.say(f".. канал: повтор снимка {trial:.2f} с, с запасом {self.delay:.2f} с (простой)")


def _quiet(transport) -> bool:
    if getattr(transport, "_pending", None):
        return False
    if getattr(transport, "_nudge_at", None) is not None:
        return False
    return True


def _emit(transport, raw: bytes, nudge: bool) -> str | None:
    """Записать пробный PING, не оставляя его в истории команд.

    Автоповтор выключен: его включает сама проба, и ровно один раз. Иначе
    история из нескольких PING уезжает вторым снимком уже во время команды.
    """
    many = getattr(transport, "_publish_many", None)
    if many is None:
        return app.send_frame(transport, raw)
    hist = list(getattr(transport, "_my_history", []))
    try:
        many([raw], keep_history=False, schedule_nudge=nudge)
    except Exception as exc:
        from .clipboard import ClipboardError
        if isinstance(exc, ClipboardError):
            return str(exc)
        raise
    transport._my_history = hist
    return None


def _take(transport, timeout: float):
    """Кадр из транспорта. None — тишина. Чужое возвращается списком байт."""
    raw = transport.receive(timeout)
    if raw is None:
        return None, None
    try:
        return framing.parse_frame(raw), raw
    except framing.FrameError:
        return None, raw


def sample(transport, delay: float, seq: int) -> str:
    """Один PING. «ok» — ответ пришёл на повтор, «fast» — ещё до него.

    Повтор делается вручную и только если к паузе ответа ещё нет. Ранний
    ответ повтор не включает: вторая ячейка стёрла бы уже принятый PONG.
    """
    held = []
    try:
        started = time.monotonic()
        fail = _emit(transport, framing.ping(seq), nudge=False)
        if fail is not None:
            return "fail"
        deadline = started + delay + _SLACK_S
        repeated = False
        while time.monotonic() < deadline:
            now = time.monotonic()
            if not repeated and now - started >= delay:
                # Первая запись так и не ушла. Одна новая ячейка, как на стенде.
                fail = _emit(transport, framing.ping(seq), nudge=False)
                if fail is not None:
                    return "fail"
                repeated = True
            left = deadline - now
            frame, raw = _take(transport, min(0.2, max(0.05, left)))
            if frame is None and raw is None:
                continue
            if frame is None:
                continue
            if _pace is not None:
                _pace.seen()
            if frame.type == config.PONG and frame.seq == seq:
                elapsed = time.monotonic() - started
                if not repeated and elapsed + 0.02 < delay:
                    return "fast"
                return "ok"
            if raw is not None:
                held.append(raw)
        return "fail"
    finally:
        if getattr(transport, "_nudge_at", None) is not None:
            transport._nudge_at = None
            transport._nudge_mine = None
        pending = getattr(transport, "_pending", None)
        if pending is not None and held:
            pending[:0] = held


def drain(transport, seconds: float = 0.4) -> None:
    """Забрать опоздавшие PONG пробы, команду в очереди не выбрасывать."""
    if getattr(transport, "_nudge_at", None) is not None:
        transport._nudge_at = None
        transport._nudge_mine = None
    deadline = time.monotonic() + seconds
    kept = []
    while time.monotonic() < deadline:
        frame, raw = _take(transport, 0.05)
        if frame is None and raw is None:
            continue
        if frame is not None and frame.type == config.PONG:
            continue
        if raw is not None:
            kept.append(raw)
    pending = getattr(transport, "_pending", None)
    if pending is not None and kept:
        pending[:0] = kept
