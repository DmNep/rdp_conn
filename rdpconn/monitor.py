"""Веб-морда канала.

Шлюз по-прежнему может отдать свою страницу флагом --monitor: её считает
сам обмен. Отдельная команда `python -m rdpconn monitor` смотрит слот
с этой машины и ничего в него не пишет, поэтому кодеру и шлюзу на RDP
не мешает. Только стандартная библиотека.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rdpconn — монитор</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font: 15px/1.4 "Segoe UI", sans-serif; background: #16181d; color: #e7e9ee; }
  header { padding: 16px 20px 8px; display: flex; gap: 16px; align-items: baseline; flex-wrap: wrap; }
  h1 { font-size: 18px; font-weight: 600; margin: 0; }
  .muted { color: #9aa3b2; }
  .pill { border-radius: 999px; padding: 2px 10px; background: #2a3140; }
  .pill.on { background: #1d4d32; color: #b6f0cf; }
  .pill.hold { background: #5a4520; color: #f3ddb0; }
  main { padding: 8px 20px 24px; }
  .rates { display: grid; grid-template-columns: repeat(4, minmax(140px, 1fr)); gap: 10px; }
  .turns { display: grid; grid-template-columns: repeat(2, minmax(180px, 1fr)); gap: 10px; margin-top: 10px; }
  h2 { font-size: 14px; font-weight: 600; margin: 18px 0 0; }
  .card { background: #1e222b; border-radius: 10px; padding: 12px 14px; }
  .card b { display: block; font-size: 22px; font-weight: 600; }
  .card .muted { display: block; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid #2a3140; }
  th { color: #9aa3b2; font-weight: 500; }
  tr.dead { color: #8b93a3; }
  ul { list-style: none; padding: 0; margin: 12px 0 0; }
  li { padding: 3px 0; color: #c5cad3; }
  .err { color: #f0b4b4; }
</style>
</head>
<body>
<header>
  <h1>rdpconn</h1>
  <span id="who" class="muted"></span>
  <span id="state" class="pill">…</span>
</header>
<main>
  <section class="rates">
    <div class="card"><span class="muted" id="up-label">гость → сайт</span><b id="up">—</b><span class="muted" id="up-avg"></span><span class="muted" id="up-max"></span><span class="muted" id="up-total"></span></div>
    <div class="card"><span class="muted" id="down-label">сайт → гость</span><b id="down">—</b><span class="muted" id="down-avg"></span><span class="muted" id="down-max"></span><span class="muted" id="down-total"></span></div>
    <div class="card"><span class="muted">в буфер</span><b id="cout">—</b><span class="muted" id="cout-avg"></span><span class="muted" id="cout-max"></span><span class="muted" id="cout-total"></span></div>
    <div class="card"><span class="muted">из буфера</span><b id="cin">—</b><span class="muted" id="cin-avg"></span><span class="muted" id="cin-max"></span><span class="muted" id="cin-total"></span></div>
  </section>
  <h2>динамический ход</h2>
  <section class="turns">
    <div class="card"><span class="muted" id="turn-up-label">ход к сайту</span><b id="turn-up">—</b><span class="muted" id="turn-up-b"></span><span class="muted" id="turn-up-last"></span></div>
    <div class="card"><span class="muted" id="turn-down-label">ход к гостю</span><b id="turn-down">—</b><span class="muted" id="turn-down-b"></span><span class="muted" id="turn-down-last"></span></div>
  </section>
  <table>
    <thead>
      <tr>
        <th>поток</th><th>куда</th><th id="col-up">к сайту</th><th id="col-down">от сайта</th><th>состояние</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <ul id="log"></ul>
  <p class="muted">Снимок буфера — не отдельный IP-пакет. В один снимок влезает около 4,6 КБ, и он уходит примерно раз в секунду.</p>
</main>
<script>
function fmt(n) {
  if (n < 1024) return n.toFixed(0) + " Б/с";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " КБ/с";
  return (n / 1024 / 1024).toFixed(2) + " МБ/с";
}
function fmtb(n) {
  if (n < 1024) return n + " Б";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " КБ";
  return (n / 1024 / 1024).toFixed(2) + " МБ";
}
function times(n) {
  const k = n % 10;
  const k100 = n % 100;
  if (k === 1 && k100 !== 11) return n + " раз";
  if (k >= 2 && k <= 4 && (k100 < 12 || k100 > 14)) return n + " раза";
  return n + " раз";
}
async function tick() {
  let data;
  try {
    const res = await fetch("/api", { cache: "no-store" });
    data = await res.json();
  } catch (err) {
    document.getElementById("state").textContent = "нет связи со шлюзом";
    document.getElementById("state").className = "pill";
    return;
  }
  const dial = data.mode === "dial";
  document.getElementById("who").textContent = data.peer || (dial ? "эта машина" : "прокси");
  const upLabel = dial ? "гость → сайт" : "программа → сайт";
  const downLabel = dial ? "сайт → гость" : "сайт → программа";
  document.getElementById("up-label").textContent = upLabel;
  document.getElementById("down-label").textContent = downLabel;
  document.getElementById("col-up").textContent = upLabel;
  document.getElementById("col-down").textContent = downLabel;
  const state = document.getElementById("state");
  state.textContent = data.hold || "работает";
  state.className = "pill " + (data.hold ? "hold" : "on");
  document.getElementById("up").textContent = fmt(data.up_s);
  document.getElementById("down").textContent = fmt(data.down_s);
  document.getElementById("cout").textContent = fmt(data.channel_out_s);
  document.getElementById("cin").textContent = fmt(data.channel_in_s);
  document.getElementById("up-avg").textContent = "10 с  " + fmt(data.up_avg);
  document.getElementById("down-avg").textContent = "10 с  " + fmt(data.down_avg);
  document.getElementById("cout-avg").textContent = "10 с  " + fmt(data.channel_out_avg);
  document.getElementById("cin-avg").textContent = "10 с  " + fmt(data.channel_in_avg);
  document.getElementById("up-max").textContent = "пик  " + fmt(data.up_max);
  document.getElementById("down-max").textContent = "пик  " + fmt(data.down_max);
  document.getElementById("cout-max").textContent = "пик  " + fmt(data.channel_out_max);
  document.getElementById("cin-max").textContent = "пик  " + fmt(data.channel_in_max);
  document.getElementById("up-total").textContent = fmtb(data.up) + " за сессию";
  document.getElementById("down-total").textContent = fmtb(data.down) + " за сессию";
  document.getElementById("cout-total").textContent = fmtb(data.channel_out) + " за сессию";
  document.getElementById("cin-total").textContent = fmtb(data.channel_in) + " за сессию";
  document.getElementById("turn-up-label").textContent = "ход к сайту";
  document.getElementById("turn-down-label").textContent = dial ? "ход к гостю" : "ход к программе";
  document.getElementById("turn-up").textContent = times(data.turn_up_n || 0);
  document.getElementById("turn-down").textContent = times(data.turn_down_n || 0);
  document.getElementById("turn-up-b").textContent = fmtb(data.turn_up_b || 0) + " на этих ходах";
  document.getElementById("turn-down-b").textContent = fmtb(data.turn_down_b || 0) + " на этих ходах";
  document.getElementById("turn-up-last").textContent = data.turn_up_last == null ? "последний  —" : "последний  " + fmtb(data.turn_up_last);
  document.getElementById("turn-down-last").textContent = data.turn_down_last == null ? "последний  —" : "последний  " + fmtb(data.turn_down_last);
  const rows = document.getElementById("rows");
  rows.replaceChildren();
  for (const row of data.streams) {
    const tr = document.createElement("tr");
    if (row.closed) tr.className = "dead";
    for (const text of ["#" + row.sid, row.target, fmtb(row.up), fmtb(row.down), row.state]) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    }
    rows.appendChild(tr);
  }
  if (!data.streams.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "muted";
    td.textContent = "потоков нет";
    tr.appendChild(td);
    rows.appendChild(tr);
  }
  const log = document.getElementById("log");
  log.replaceChildren();
  for (const line of data.events) {
    const li = document.createElement("li");
    li.textContent = line;
    log.appendChild(li);
  }
}
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


class Face:
    """Снимок шлюза для страницы. Пишет один поток, читает сервер морды."""

    def __init__(self, mode: str, peer: str) -> None:
        self.mode = mode
        self.peer = peer
        self._lock = threading.Lock()
        self._streams: dict[int, dict] = {}
        self._events: list[str] = []
        self.up = 0
        self.down = 0
        self.channel_out = 0
        self.channel_in = 0
        self.hold = ""
        self._t = time.monotonic()
        self._up = 0
        self._down = 0
        self._cout = 0
        self._cin = 0
        self._up_s = 0.0
        self._down_s = 0.0
        self._cout_s = 0.0
        self._cin_s = 0.0
        self._up_max = 0.0
        self._down_max = 0.0
        self._cout_max = 0.0
        self._cin_max = 0.0
        self._samples: list[tuple[float, int, int, int, int]] = []
        self._burst: dict[tuple[int, str], tuple[int, float]] = {}
        self.turn_up_n = 0
        self.turn_down_n = 0
        self.turn_up_b = 0
        self.turn_down_b = 0
        self.turn_up_last: int | None = None
        self.turn_down_last: int | None = None

    def open(self, sid: int, target: str) -> None:
        with self._lock:
            row = self._streams.get(sid)
            if row is not None and not row["closed"]:
                row["target"] = target
                return
            self._streams[sid] = {
                "sid": sid,
                "target": target,
                "up": 0,
                "down": 0,
                "ready": False,
                "closed": False,
                "why": "",
                "touch": time.monotonic(),
                "closed_at": 0.0,
            }
            who = "гость" if self.mode == "dial" else "программа"
            self._event(f"поток {sid}  {who} → {target}")

    def add(self, sid: int, up: int = 0, down: int = 0) -> None:
        if up <= 0 and down <= 0:
            return
        with self._lock:
            row = self._streams.get(sid)
            if row is None:
                row = {
                    "sid": sid,
                    "target": "?",
                    "up": 0,
                    "down": 0,
                    "ready": True,
                    "closed": False,
                    "why": "",
                    "touch": time.monotonic(),
                    "closed_at": 0.0,
                }
                self._streams[sid] = row
            row["up"] += up
            row["down"] += down
            row["ready"] = True
            row["touch"] = time.monotonic()
            self.up += up
            self.down += down
            if up:
                self._note_burst(sid, row, "up", up)
            if down:
                self._note_burst(sid, row, "down", down)

    def ready(self, sid: int) -> None:
        with self._lock:
            row = self._streams.get(sid)
            if row is None or row["closed"]:
                return
            row["ready"] = True
            row["touch"] = time.monotonic()

    def close(self, sid: int, why: str) -> None:
        with self._lock:
            row = self._streams.get(sid)
            if row is None or row["closed"]:
                return
            row["closed"] = True
            row["why"] = why
            row["closed_at"] = time.monotonic()
            self._event(f"поток {sid} {why}")

    def channel(self, out_n: int = 0, in_n: int = 0) -> None:
        if out_n <= 0 and in_n <= 0:
            return
        with self._lock:
            self.channel_out += max(0, out_n)
            self.channel_in += max(0, in_n)

    def turn(self, kind: str, n: int) -> None:
        """Лишний снимок. gave — ход отдали собеседнику, took — собеседник отдал нам.

        На машине с интернетом отданный ход везёт байты гостя к сайту,
        взятый ход везёт ответ сайта к гостю.
        """
        n = max(0, n)
        with self._lock:
            if self.mode == "dial":
                toward_site = kind == "gave"
            else:
                toward_site = kind == "took"
            if toward_site:
                self.turn_up_n += 1
                self.turn_up_b += n
                self.turn_up_last = n
                where = "к сайту"
            else:
                self.turn_down_n += 1
                self.turn_down_b += n
                self.turn_down_last = n
                where = "к гостю" if self.mode == "dial" else "к программе"
            self._event(f"ход {where}  {n} Б")

    def hold_text(self, text: str) -> None:
        with self._lock:
            self.hold = text

    def _way_text(self, row: dict, way: str, n: int) -> str:
        target = row["target"]
        if self.mode == "dial":
            if way == "up":
                return f"гость → {target}  {n} Б"
            return f"{target} → гость  {n} Б"
        if way == "up":
            return f"программа → {target}  {n} Б"
        return f"{target} → программа  {n} Б"

    def _note_burst(self, sid: int, row: dict, way: str, n: int) -> None:
        key = (sid, way)
        acc, last = self._burst.get(key, (0, time.monotonic()))
        acc += n
        now = time.monotonic()
        if now - last < 1.0 and acc < 8192:
            self._burst[key] = (acc, last)
            return
        self._burst[key] = (0, now)
        self._event(self._way_text(row, way, acc))

    def _flush_bursts(self, now: float) -> None:
        for key, (acc, last) in list(self._burst.items()):
            if not acc or now - last < 1.0:
                continue
            sid, way = key
            row = self._streams.get(sid)
            self._burst[key] = (0, now)
            if row is not None:
                self._event(self._way_text(row, way, acc))

    def _event(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._events.insert(0, f"{stamp}  {text}")
        del self._events[40:]

    def _remember(self, now: float) -> None:
        """Текущая скорость, пик и точка для среднего за 10 с.

        Пик берётся по шагу около секунды. Долгая пауза между опросами
        страницы в пик не идёт: иначе тишина выглядит как очень медленный рекорд.
        """
        dt = now - self._t
        if 0.4 <= dt <= 2.5:
            self._up_s = (self.up - self._up) / dt
            self._down_s = (self.down - self._down) / dt
            self._cout_s = (self.channel_out - self._cout) / dt
            self._cin_s = (self.channel_in - self._cin) / dt
            self._up_max = max(self._up_max, self._up_s)
            self._down_max = max(self._down_max, self._down_s)
            self._cout_max = max(self._cout_max, self._cout_s)
            self._cin_max = max(self._cin_max, self._cin_s)
            self._t = now
            self._up = self.up
            self._down = self.down
            self._cout = self.channel_out
            self._cin = self.channel_in
        elif dt > 2.5:
            self._up_s = self._down_s = self._cout_s = self._cin_s = 0.0
            self._t = now
            self._up = self.up
            self._down = self.down
            self._cout = self.channel_out
            self._cin = self.channel_in
        self._samples.append((now, self.up, self.down, self.channel_out, self.channel_in))
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
        start = self._samples[0]
        span = now - start[0]
        if span <= 0:
            return 0.0
        return (self._samples[-1][index] - start[index]) / span

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            self._flush_bursts(now)
            self._remember(now)
            fresh = []
            for row in self._streams.values():
                if row["closed"] and now - row["closed_at"] > 30:
                    continue
                fresh.append(row)
            self._streams = {row["sid"]: row for row in fresh}
            streams = []
            for row in sorted(fresh, key=lambda item: item["sid"]):
                if row["closed"]:
                    state = row["why"] or "закрыт"
                elif not row.get("ready"):
                    state = "соединяется"
                elif now - row["touch"] > 3:
                    state = "молчит"
                else:
                    state = "идёт"
                streams.append({
                    "sid": row["sid"],
                    "target": row["target"],
                    "up": row["up"],
                    "down": row["down"],
                    "closed": row["closed"],
                    "state": state,
                })
            return {
                "mode": self.mode,
                "peer": self.peer,
                "hold": self.hold,
                "up_s": self._up_s,
                "down_s": self._down_s,
                "channel_out_s": self._cout_s,
                "channel_in_s": self._cin_s,
                "up_avg": self._window(now, 1),
                "down_avg": self._window(now, 2),
                "channel_out_avg": self._window(now, 3),
                "channel_in_avg": self._window(now, 4),
                "up_max": self._up_max,
                "down_max": self._down_max,
                "channel_out_max": self._cout_max,
                "channel_in_max": self._cin_max,
                "up": self.up,
                "down": self.down,
                "channel_out": self.channel_out,
                "channel_in": self.channel_in,
                "turn_up_n": self.turn_up_n,
                "turn_down_n": self.turn_down_n,
                "turn_up_b": self.turn_up_b,
                "turn_down_b": self.turn_down_b,
                "turn_up_last": self.turn_up_last,
                "turn_down_last": self.turn_down_last,
                "streams": streams,
                "events": list(self._events),
            }


_local = threading.local()


def bind(face: Face | None) -> None:
    _local.face = face


def current() -> Face | None:
    return getattr(_local, "face", None)


def serve(face: Face, host: str, port: int, page: str | None = None) -> None:
    """Поднять страницу. Ошибка порта не останавливает шлюз."""
    html = _PAGE if page is None else page

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api"):
                body = json.dumps(face.snapshot()).encode("utf-8")
                kind = "application/json; charset=utf-8"
            else:
                body = html.encode("utf-8")
                kind = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if self.path.startswith("/api/peaks"):
                reset = getattr(face, "reset_peaks", None)
                if reset is not None:
                    reset()
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            return

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        from . import app
        app.warn(f"!! монитор {host}:{port} не поднялся: {exc}")
        return False
    from . import app
    app.say(f"монитор http://{host}:{port}")
    thread = threading.Thread(target=httpd.serve_forever, name="rdpconn-monitor", daemon=True)
    thread.start()
    return True


_WATCH_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rdpconn — монитор</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font: 15px/1.4 "Segoe UI", sans-serif; background: #16181d; color: #e7e9ee; }
  header { padding: 16px 20px 8px; display: flex; gap: 16px; align-items: baseline; flex-wrap: wrap; }
  h1 { font-size: 18px; font-weight: 600; margin: 0; }
  .muted { color: #9aa3b2; }
  .pill { border-radius: 999px; padding: 2px 10px; background: #2a3140; }
  .pill.on { background: #1d4d32; color: #b6f0cf; }
  .pill.hold { background: #5a4520; color: #f3ddb0; }
  main { padding: 8px 20px 24px; }
  .rates { display: grid; grid-template-columns: repeat(2, minmax(180px, 1fr)); gap: 10px; }
  .card { background: #1e222b; border-radius: 10px; padding: 12px 14px; }
  .card b { display: block; font-size: 22px; font-weight: 600; }
  .card .muted { display: block; }
  button { margin-top: 14px; background: #2a3140; color: #e7e9ee; border: 0; border-radius: 8px; padding: 8px 14px; font: inherit; cursor: pointer; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid #2a3140; }
  th { color: #9aa3b2; font-weight: 500; }
  ul { list-style: none; padding: 0; margin: 12px 0 0; }
  li { padding: 3px 0; color: #c5cad3; }
</style>
</head>
<body>
<header>
  <h1>rdpconn</h1>
  <span id="who" class="muted"></span>
  <span id="state" class="pill">…</span>
</header>
<main>
  <section class="rates">
    <div class="card"><span class="muted">в рдп</span><b id="into">—</b><span class="muted" id="into-avg"></span><span class="muted" id="into-max"></span><span class="muted" id="into-total"></span></div>
    <div class="card"><span class="muted">из рдп</span><b id="from">—</b><span class="muted" id="from-avg"></span><span class="muted" id="from-max"></span><span class="muted" id="from-total"></span></div>
  </section>
  <section class="rates" style="margin-top:10px">
    <div class="card"><span class="muted">режим</span><b id="pack" style="font-size:16px">—</b></div>
    <div class="card"><span class="muted">слот</span><b id="slot">—</b><span class="muted" id="frames"></span></div>
  </section>
  <button id="reset" type="button">сбросить пики</button>
  <table>
    <thead><tr><th>сторона</th><th>кадров</th><th>байт</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <ul id="log"></ul>
  <p class="muted">Страница только читает буфер этой машины. В слот она не пишет и обмен на RDP не занимает.</p>
</main>
<script>
function fmt(n) {
  if (n < 1024) return n.toFixed(0) + " Б/с";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " КБ/с";
  return (n / 1024 / 1024).toFixed(2) + " МБ/с";
}
function fmtb(n) {
  if (n < 1024) return n + " Б";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " КБ";
  return (n / 1024 / 1024).toFixed(2) + " МБ";
}
async function tick() {
  let data;
  try {
    const res = await fetch("/api", { cache: "no-store" });
    data = await res.json();
  } catch (err) {
    document.getElementById("state").textContent = "нет страницы";
    document.getElementById("state").className = "pill";
    return;
  }
  document.getElementById("who").textContent = data.peer || "";
  const state = document.getElementById("state");
  state.textContent = data.hold || "смотрит";
  state.className = "pill " + (data.hold ? "hold" : "on");
  document.getElementById("into").textContent = fmt(data.into_s || 0);
  document.getElementById("into-avg").textContent = "10 с  " + fmt(data.into_avg || 0);
  document.getElementById("into-max").textContent = "пик  " + fmt(data.into_max || 0);
  document.getElementById("into-total").textContent = fmtb(data.into || 0) + " за сессию";
  document.getElementById("from").textContent = fmt(data.from_s || 0);
  document.getElementById("from-avg").textContent = "10 с  " + fmt(data.from_avg || 0);
  document.getElementById("from-max").textContent = "пик  " + fmt(data.from_max || 0);
  document.getElementById("from-total").textContent = fmtb(data.from || 0) + " за сессию";
  document.getElementById("pack").textContent = data.pack || "по одному";
  document.getElementById("frames").textContent = (data.frames || 0) + " кадров";
  document.getElementById("slot").textContent = data.slot || "—";
  const rows = document.getElementById("rows");
  rows.replaceChildren();
  const peers = data.peers || [];
  if (!peers.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "muted";
    td.textContent = "сторон пока нет";
    tr.appendChild(td);
    rows.appendChild(tr);
  }
  for (const row of peers) {
    const tr = document.createElement("tr");
    for (const text of [row.id, String(row.frames), fmtb(row.bytes)]) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    }
    rows.appendChild(tr);
  }
  const log = document.getElementById("log");
  log.replaceChildren();
  for (const line of data.events || []) {
    const li = document.createElement("li");
    li.textContent = line;
    log.appendChild(li);
  }
}
document.getElementById("reset").onclick = async () => {
  await fetch("/api/peaks", { method: "POST" });
  tick();
};
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


def _cells(text: str):
    """Ячейки слота, без записи и без состояния транспорта."""
    import base64
    import binascii

    parts = text.split("|")
    if len(parts) < 2 or parts[0] != "RDP1":
        return
    for part in parts[1:]:
        fields = part.split(":")
        if len(fields) != 3:
            continue
        who, num, b64 = fields
        if not who or not num.isdigit():
            continue
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            continue
        yield who, int(num), b64, raw


def _frame_label(raw: bytes) -> tuple[str, int]:
    """Короткая подпись кадра. Текст запроса и ответа модели сюда не входит."""
    from . import coder, config, framing, gate

    size = len(raw)
    try:
        frame = framing.parse_frame(raw)
    except framing.FrameError:
        return "битый кадр", size
    if frame.type != config.NOTE:
        return frame.type_name, size
    item = gate.unpack(frame.payload)
    if item is not None:
        op, sid, _offset, body = item
        if op == gate.OP_OPEN and len(body) >= 2:
            port = int.from_bytes(body[:2], "big")
            host = body[2:].decode("utf-8", errors="replace")
            return f"шлюз {sid} → {host}:{port}", size
        if op == gate.OP_DATA:
            return f"шлюз {sid} данные", size
        names = {
            gate.OP_OK: "готов",
            gate.OP_FAIL: "отказ",
            gate.OP_END: "конец",
            gate.OP_ACK: "подтверждение",
            gate.OP_POLL: "опрос",
            gate.OP_HALT: "пауза",
            gate.OP_GO: "дальше",
            gate.OP_YIELD: "ход",
        }
        return f"шлюз {sid} {names.get(op, str(op))}", size
    item = coder.decode(frame.payload)
    if item is None:
        return "NOTE", size
    op, flags, _mid, _part, body = item
    if op == coder.OP_PIECE:
        if flags & coder.FLAG_ALONE:
            kind = "ответ"
        elif flags & coder.FLAG_WINDOW:
            kind = "запрос"
        else:
            kind = "кусок"
        return f"кодер {kind}", size
    names = {
        coder.OP_ACK: "подтверждение",
        coder.OP_END: "конец",
        coder.OP_ERR: "ошибка",
    }
    return f"кодер {names.get(op, str(op))}", size


class Watch:
    """Что видно в слоте с этой машины. Пишет поток опроса, читает страница."""

    def __init__(self, local: str = "A") -> None:
        self.local = local
        self._lock = threading.Lock()
        self._seen: set[tuple] = set()
        self._order: list[tuple] = []
        self._last_keys: set[tuple] = set()
        self._peers: dict[str, dict] = {}
        self._events: list[str] = []
        self._prev_blob: dict[str, tuple] = {}
        self._prev_at: dict[str, float] = {}
        self.total = 0
        self.into = 0
        self.out = 0
        self.frames = 0
        self.hold = ""
        self.slot = "пусто"
        self.pack = "по одному"
        self._t = time.monotonic()
        self._into_mark = 0
        self._out_mark = 0
        self._into_s = 0.0
        self._out_s = 0.0
        self._into_max = 0.0
        self._out_max = 0.0
        self._samples: list[tuple[float, int, int]] = []

    def busy(self, on: bool) -> None:
        with self._lock:
            if on:
                self.hold = "буфер занят"
            elif self.hold == "буфер занят":
                self.hold = ""

    def note(self, text: str | None, seed: bool = False) -> None:
        with self._lock:
            self._apply(text or "", seed)

    def reset_peaks(self) -> None:
        with self._lock:
            self._into_max = 0.0
            self._out_max = 0.0

    def _remember_key(self, key: tuple) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        self._order.append(key)
        if len(self._order) > 500:
            self._seen.discard(self._order.pop(0))
        return True

    def _apply(self, text: str, seed: bool) -> None:
        if not text:
            if not seed and self.slot != "пусто":
                self._event("слот пуст")
            self.slot = "пусто"
            self._last_keys = set()
            if self.hold == "чужая копия":
                self.hold = ""
            return
        if not text.startswith("RDP1|"):
            if not seed and self.slot != "чужая копия":
                self._event(f"чужая копия  {len(text)} символов")
            self.slot = "чужая копия"
            self.hold = "чужая копия"
            self._last_keys = set()
            return
        if self.hold == "чужая копия":
            self.hold = ""
        self.slot = "обмен"
        fresh: list[tuple[str, str, int, str]] = []
        keys = set()
        for who, num, b64, raw in _cells(text):
            key = (who, num, b64)
            keys.add(key)
            if key in self._last_keys or not self._remember_key(key):
                continue
            if seed:
                continue
            label, size = _frame_label(raw)
            fresh.append((who, label, size, b64))
        self._last_keys = keys
        if seed or not fresh:
            return
        grouped: dict[tuple[str, str], list[int]] = {}
        blobs: dict[str, list[str]] = {}
        for who, label, size, b64 in fresh:
            row = self._peers.setdefault(who, {"id": who, "frames": 0, "bytes": 0})
            row["frames"] += 1
            row["bytes"] += size
            self.total += size
            self.frames += 1
            if who == self.local:
                self.into += size
            else:
                self.out += size
            blobs.setdefault(who, []).append(b64)
            bucket = grouped.setdefault((who, label), [0, 0])
            bucket[0] += 1
            bucket[1] += size
        self.pack = self._pack(blobs)
        for (who, label), (count, size) in grouped.items():
            extra = f"  ×{count}" if count > 1 else ""
            self._event(f"{who}  {label}  {size} Б{extra}")
        if self.pack != "по одному":
            self._event(self.pack)

    def _pack(self, blobs: dict[str, list[str]]) -> str:
        """Уплотнение — несколько ячеек одним снимком. Два пакета — тот же снимок новым номером."""
        now = time.monotonic()
        bits = []
        for who, items in blobs.items():
            sign = tuple(sorted(items))
            prev = self._prev_blob.get(who)
            prev_at = self._prev_at.get(who, 0.0)
            repeat = prev is not None and sign == prev and now - prev_at < 2.0
            packed = len(items) >= 2
            self._prev_blob[who] = sign
            self._prev_at[who] = now
            side = "в рдп" if who == self.local else "из рдп"
            if packed and repeat:
                bits.append(f"{side}: уплотнение, {len(items)} ячеек, два пакета")
            elif packed:
                bits.append(f"{side}: уплотнение, {len(items)} ячеек")
            elif repeat:
                bits.append(f"{side}: два пакета")
        return " · ".join(bits) if bits else "по одному"

    def _event(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._events.insert(0, f"{stamp}  {text}")
        del self._events[40:]

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
            peers = [dict(row) for row in sorted(self._peers.values(), key=lambda item: item["id"])]
            return {
                "mode": "watch",
                "peer": "эта машина, только чтение",
                "hold": self.hold,
                "slot": self.slot,
                "pack": self.pack,
                "up": self.total,
                "into": self.into,
                "from": self.out,
                "into_s": self._into_s,
                "from_s": self._out_s,
                "into_avg": self._window(now, 1),
                "from_avg": self._window(now, 2),
                "into_max": self._into_max,
                "from_max": self._out_max,
                "frames": self.frames,
                "peers": peers,
                "events": list(self._events),
            }


def _loop(store, watch: Watch) -> None:
    """Номер слота без открытия. Текст — одним OpenClipboard, и только когда номер сдвинулся.

    После сдвига номер иногда обгоняет текст. Пока строка та же, открываем
    ещё несколько раз по одному разу, а не держим буфер повторами внутри.
    """
    from .clipboard import ClipboardError

    last = None
    shown = None
    seeded = False
    waiting = False
    misses = 0
    stale = 0
    while True:
        seq = store.seq()
        if seeded and not waiting and seq == last:
            time.sleep(0.1)
            continue
        try:
            text = store.try_read()
        except ClipboardError:
            misses += 1
            waiting = True
            if misses >= 5:
                watch.busy(True)
            time.sleep(0.1)
            continue
        misses = 0
        watch.busy(False)
        if not seeded:
            watch.note(text, seed=True)
            seeded = True
            shown = text
            last = seq
            waiting = False
            stale = 0
            time.sleep(0.05)
            continue
        if text == shown and stale < 8:
            stale += 1
            waiting = True
            time.sleep(0.1)
            continue
        stale = 0
        waiting = False
        last = seq
        shown = text
        watch.note(text)
        time.sleep(0.05)


def main(argv=None) -> int:
    """Смотреть слот с этой машины. Записи нет."""
    import argparse

    from . import app, clipboard

    parser = argparse.ArgumentParser(
        prog="rdpconn.monitor",
        description="Страница наблюдения за слотом. В буфер не пишет.",
    )
    parser.add_argument("--bind", default="127.0.0.1:8078",
                        help="страница на этой машине (по умолчанию 127.0.0.1:8078)")
    parser.add_argument("--peer-id", default="A",
                        help="метка этой машины: её кадры считаются трафиком в рдп")
    args = parser.parse_args(argv)
    host, sep, port_s = args.bind.rpartition(":")
    if not sep:
        app.warn(f"!! монитор: нужен адрес вида 127.0.0.1:8078, получено {args.bind}")
        return 2
    try:
        port = int(port_s)
    except ValueError:
        app.warn(f"!! монитор: порт не число в {args.bind}")
        return 2
    try:
        store = clipboard.open_store("")
    except clipboard.ClipboardError as exc:
        app.warn(str(exc))
        return 2
    watch = Watch(local=args.peer_id or "A")
    if not serve(watch, host or "127.0.0.1", port, page=_WATCH_PAGE):
        return 1
    app.say("только чтение: слот не пишу")
    try:
        _loop(store, watch)
    except KeyboardInterrupt:
        app.say("")
    return 0
