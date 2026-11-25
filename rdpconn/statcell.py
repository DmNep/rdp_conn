"""Одна служебная ячейка снимка гуеста.

Кадр тот же NOTE, потолок 240 байт тот же. Внутри payload — магия S1,
не новый тип кадра. Журнала нет: очередь, сколько байт ещё ждёт,
сколько процессов сидит на локальном прокси, режим, код последней
ошибки и сколько ячеек было в прошлом снимке.

Ячейку кладёт только гуест, поверх своих данных, самым новым номером.
Хост снимает её до кодера и шлюза и рисует страницу у себя.
"""

from __future__ import annotations

import threading
import time

from . import config, framing

MAGIC = b"S1"
_VER = 1
# magic + ver + feed + err + procs + queued + prev
_BODY = 2 + 1 + 1 + 2 + 2 + 4 + 2

FEED_MODEL = 1
FEED_NET = 2
FEED_NAME = {FEED_MODEL: "модель", FEED_NET: "интернет"}

ERR_OK = 0
ERR_SEND = 1
ERR_CODER = 2
ERR_JSON = 3
ERR_NAME = {
    ERR_OK: "",
    ERR_SEND: "снимок не ушёл",
    ERR_CODER: "кодер",
    ERR_JSON: "не json",
}


def is_frame(raw: bytes) -> bool:
    """Этот кадр — служебная ячейка, а не данные кодера или шлюза."""
    try:
        frame = framing.parse_frame(raw)
    except framing.FrameError:
        return False
    return frame.type == config.NOTE and frame.payload.startswith(MAGIC)


def pack(feed: int, procs: int, queued: int, prev: int, err: int = 0) -> bytes:
    body = (
        MAGIC
        + bytes((_VER, feed & 0xFF))
        + _u16(err)
        + _u16(procs)
        + _u32(queued)
        + _u16(prev)
    )
    if len(body) != _BODY:
        raise ValueError(f"ячейка статистики {len(body)} байт, нужна {_BODY}")
    return framing.build_frame(config.NOTE, 0, body)


def unpack(raw: bytes) -> dict | None:
    try:
        frame = framing.parse_frame(raw)
    except framing.FrameError:
        return None
    body = frame.payload
    if frame.type != config.NOTE or len(body) != _BODY or body[:2] != MAGIC:
        return None
    if body[2] != _VER:
        return None
    return {
        "feed": body[3],
        "err": int.from_bytes(body[4:6], "big"),
        "procs": int.from_bytes(body[6:8], "big"),
        "queued": int.from_bytes(body[8:12], "big"),
        "prev": int.from_bytes(body[12:14], "big"),
    }


def _u16(value: int) -> bytes:
    return max(0, min(int(value), 0xFFFF)).to_bytes(2, "big")


def _u32(value: int) -> bytes:
    return max(0, min(int(value), 0xFFFFFFFF)).to_bytes(4, "big")


class Board:
    """Цифры гуеста. В ячейку попадает прошлый снимок, не журнал."""

    def __init__(self, feed: int) -> None:
        self.feed = feed
        self.procs = 0
        self.queued = 0
        self.sending = 0
        self.prev_cells = 0
        self.err = ERR_OK
        self._reported = False

    def note(self, cells: int, queued: int, procs: int | None = None) -> None:
        """Зафиксировать снимок, который сейчас уйдёт. Прошлый остаётся в ячейке."""
        if self._reported:
            self.err = ERR_OK
            self._reported = False
        self.prev_cells = self.sending
        self.sending = max(0, int(cells))
        self.queued = max(0, int(queued))
        if procs is not None:
            self.procs = max(0, int(procs))

    def fail(self, code: int) -> None:
        """Ошибка после снимка. Её унесёт следующий, в том числе повтор того же."""
        self.err = code
        self._reported = False

    def frame(self) -> bytes:
        self._reported = True
        return pack(self.feed, self.procs, self.queued, self.prev_cells, self.err)


def attach_guest(transport, feed: int) -> Board:
    """Гуест кладёт ячейку в каждый свой снимок. Повторный вызов ту же доску не затирает."""
    board = getattr(transport, "board", None)
    if board is None:
        board = Board(feed)
        transport.board = board
        transport.stats_frame = board.frame
    return board


class Picture:
    """Сводка на хосте: свои байты и последняя ячейка гуеста."""

    def __init__(self, feed: str) -> None:
        self.feed = feed
        self.into = 0
        self.out = 0
        self._into_s = 0.0
        self._out_s = 0.0
        self._into_max = 0.0
        self._out_max = 0.0
        self._t = time.monotonic()
        self._into_mark = 0
        self._out_mark = 0
        self._samples: list[tuple[float, int, int]] = []
        self.guest: dict | None = None
        self.guest_at = 0.0
        self._lock = threading.Lock()

    def add_into(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self.into += n
            self._remember(time.monotonic())

    def add_from(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self.out += n
            self._remember(time.monotonic())

    def note_guest(self, info: dict) -> None:
        with self._lock:
            self.guest = dict(info)
            self.guest_at = time.monotonic()

    def reset_peaks(self) -> None:
        with self._lock:
            self._into_max = 0.0
            self._out_max = 0.0

    def _remember(self, now: float) -> None:
        dt = now - self._t
        if 0.4 <= dt <= 2.5:
            self._into_s = (self.into - self._into_mark) / dt
            self._out_s = (self.out - self._out_mark) / dt
            self._into_max = max(self._into_max, self._into_s)
            self._out_max = max(self._out_max, self._out_s)
            self._t = now
            self._into_mark = self.into
            self._out_mark = self.out
        elif dt > 2.5:
            self._into_s = self._out_s = 0.0
            self._t = now
            self._into_mark = self.into
            self._out_mark = self.out
        self._samples.append((now, self.into, self.out))
        cutoff = now - 10.0
        keep = 0
        for index, sample in enumerate(self._samples):
            if sample[0] >= cutoff:
                keep = max(0, index - 1)
                break
        if keep:
            del self._samples[:keep]

    def _window(self, now: float, index: int) -> float:
        if not self._samples:
            return 0.0
        span = now - self._samples[0][0]
        if span <= 0:
            return 0.0
        return (self._samples[-1][index] - self._samples[0][index]) / span

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            self._remember(now)
            guest = self.guest
            age = None if not self.guest_at else round(now - self.guest_at, 1)
            feed = FEED_NAME.get(guest["feed"], "нет") if guest else "нет"
            err = ERR_NAME.get(guest["err"], str(guest["err"])) if guest else ""
            return {
                "mode": "host",
                "feed": "модель" if self.feed == "model" else "интернет",
                "into": self.into,
                "from": self.out,
                "into_s": self._into_s,
                "from_s": self._out_s,
                "into_avg": self._window(now, 1),
                "from_avg": self._window(now, 2),
                "into_max": self._into_max,
                "from_max": self._out_max,
                "guest_feed": feed,
                "guest_procs": 0 if guest is None else guest["procs"],
                "guest_queued": 0 if guest is None else guest["queued"],
                "guest_prev": 0 if guest is None else guest["prev"],
                "guest_err": err,
                "guest_age": age,
            }


PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rdpconn — хост</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font: 15px/1.4 "Segoe UI", sans-serif; background: #16181d; color: #e7e9ee; }
  header { padding: 16px 20px 8px; display: flex; gap: 16px; align-items: baseline; flex-wrap: wrap; }
  h1 { font-size: 18px; font-weight: 600; margin: 0; }
  .muted { color: #9aa3b2; }
  main { padding: 8px 20px 24px; }
  .rates { display: grid; grid-template-columns: repeat(3, minmax(180px, 1fr)); gap: 10px; }
  .card { background: #1e222b; border-radius: 10px; padding: 12px 14px; }
  .card b { display: block; font-size: 22px; font-weight: 600; }
  .card .muted { display: block; }
  button { margin-top: 14px; background: #2a3140; color: #e7e9ee; border: 0; border-radius: 8px; padding: 8px 14px; font: inherit; cursor: pointer; }
</style>
</head>
<body>
<header>
  <h1>rdpconn</h1>
  <span id="feed" class="muted"></span>
</header>
<main>
  <div class="rates">
    <div class="card"><span class="muted">в рдп</span><b id="into">0</b><span class="muted" id="into_more"></span></div>
    <div class="card"><span class="muted">из рдп</span><b id="from">0</b><span class="muted" id="from_more"></span></div>
    <div class="card"><span class="muted">гуест</span><b id="guest">нет</b><span class="muted" id="guest_more"></span></div>
  </div>
  <button id="reset" type="button">сбросить пики</button>
</main>
<script>
function fmt(n) {
  n = Math.round(n || 0);
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " МБ";
  if (n >= 1024) return (n / 1024).toFixed(1) + " КБ";
  return n + " Б";
}
function rate(cur, avg, peak) {
  return fmt(cur) + "/с, среднее " + fmt(avg) + "/с, пик " + fmt(peak) + "/с";
}
async function tick() {
  const r = await fetch("/api", {cache: "no-store"});
  const s = await r.json();
  document.getElementById("feed").textContent = s.feed;
  document.getElementById("into").textContent = fmt(s.into);
  document.getElementById("from").textContent = fmt(s.from);
  document.getElementById("into_more").textContent = rate(s.into_s, s.into_avg, s.into_max);
  document.getElementById("from_more").textContent = rate(s.from_s, s.from_avg, s.from_max);
  document.getElementById("guest").textContent = s.guest_feed;
  const age = s.guest_age == null ? "снимка ещё не было" : ("снимок " + s.guest_age + " с назад");
  const err = s.guest_err ? (", ошибка: " + s.guest_err) : "";
  document.getElementById("guest_more").textContent =
    "процессов " + s.guest_procs
    + ", ждёт " + fmt(s.guest_queued)
    + ", прошлый снимок " + s.guest_prev + " яч., "
    + age + err;
}
document.getElementById("reset").onclick = async () => {
  await fetch("/api/peaks", {method: "POST"});
  tick();
};
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""
