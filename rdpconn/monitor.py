"""Веб-морда шлюза: скорость, потоки, кто с кем говорит.

Только стандартная библиотека. Слушает localhost и отдаёт одну страницу.
Счётчики пишет поток шлюза, страницу читает браузер.
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
  .card { background: #1e222b; border-radius: 10px; padding: 12px 14px; }
  .card b { display: block; font-size: 22px; font-weight: 600; }
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
    <div class="card"><span class="muted" id="up-label">гость → сайт</span><b id="up">—</b><span class="muted" id="up-total"></span></div>
    <div class="card"><span class="muted" id="down-label">сайт → гость</span><b id="down">—</b><span class="muted" id="down-total"></span></div>
    <div class="card"><span class="muted">в буфер</span><b id="cout">—</b><span class="muted" id="cout-total"></span></div>
    <div class="card"><span class="muted">из буфера</span><b id="cin">—</b><span class="muted" id="cin-total"></span></div>
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
  document.getElementById("up-total").textContent = fmtb(data.up) + " за сессию";
  document.getElementById("down-total").textContent = fmtb(data.down) + " за сессию";
  document.getElementById("cout-total").textContent = fmtb(data.channel_out) + " за сессию";
  document.getElementById("cin-total").textContent = fmtb(data.channel_in) + " за сессию";
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
        self._burst: dict[tuple[int, str], tuple[int, float]] = {}

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

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            self._flush_bursts(now)
            dt = now - self._t
            if dt >= 0.4:
                self._up_s = (self.up - self._up) / dt
                self._down_s = (self.down - self._down) / dt
                self._cout_s = (self.channel_out - self._cout) / dt
                self._cin_s = (self.channel_in - self._cin) / dt
                self._t = now
                self._up = self.up
                self._down = self.down
                self._cout = self.channel_out
                self._cin = self.channel_in
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
                "up": self.up,
                "down": self.down,
                "channel_out": self.channel_out,
                "channel_in": self.channel_in,
                "streams": streams,
                "events": list(self._events),
            }


_local = threading.local()


def bind(face: Face | None) -> None:
    _local.face = face


def current() -> Face | None:
    return getattr(_local, "face", None)


def serve(face: Face, host: str, port: int) -> None:
    """Поднять страницу. Ошибка порта не останавливает шлюз."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api"):
                body = json.dumps(face.snapshot()).encode("utf-8")
                kind = "application/json; charset=utf-8"
            else:
                body = _PAGE.encode("utf-8")
                kind = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", kind)
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
        return
    from . import app
    app.say(f"монитор http://{host}:{port}")
    thread = threading.Thread(target=httpd.serve_forever, name="rdpconn-monitor", daemon=True)
    thread.start()
