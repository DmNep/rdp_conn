"""Ожидание ACK у tx: пока heartbeat жив, порог общий с ним, а не отдельный
короткий таймер (--wait x --retries, docs/protocol.md 5).

Раньше tx сдавался через --wait x --retries (по умолчанию 1 с x 3 = 3 с) даже
если собеседник жив и просто отвечает дольше — ровно то, что происходит на
живом RDP-редиректе (AGENTS.md 3,6: лаг 10-15 с). Три сценария:

  1. Канал с задержкой больше старого бюджета (sim:), но heartbeat включён —
     команда всё равно проходит, а не отбивается через 3 с.
  2. Собеседника нет вовсе, heartbeat включён с маленьким --ping — tx сдаётся
     по молчанию heartbeat, не виснет.
  3. Heartbeat выключен (--ping 0) — старое поведение: короткий бюджет
     --wait x --retries, как было всегда (локальные проверки, отвечать некому).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PY = sys.executable
ENV = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")

_failed = 0


def check(name, ok, detail="") -> None:
    global _failed
    if ok:
        print(f"  {name}: OK  {detail}".rstrip())
    else:
        _failed += 1
        print(f"  {name}: FAIL  {detail}".rstrip())


def run(args, timeout):
    proc = subprocess.run(
        [PY, "-m", args[0]] + args[1:], cwd=ROOT, env=ENV,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    return proc.stdout, proc.stderr, proc.returncode


def start(args):
    return subprocess.Popen(
        [PY, "-m", args[0]] + args[1:], cwd=ROOT, env=ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )


print("=== 1. задержка больше старого бюджета (3 с), heartbeat жив — проходит ===")
slot = os.path.join(tempfile.mkdtemp(prefix="rdpconn_wait1_"), "slot.txt")
rx = start(["rdpconn.rx", "--slot", f"sim:5,1:{slot}",
            "--peer-id", "B", "--exec", "--duration", "40"])
time.sleep(0.5)
t0 = time.monotonic()
out, err, code = run(
    ["rdpconn.tx", "--slot", f"sim:5,1:{slot}",
     "--peer-id", "A", "--wait", "1", "--retries", "3",
     "--reply-wait", "4", "echo perezhivaet_zaderzhku"],
    timeout=40,
)
elapsed = time.monotonic() - t0
check("tx дождался ответа, а не сдался за 3 с", code == 0 and "perezhivaet_zaderzhku" in out,
      f"code={code} elapsed={elapsed:.1f}s out={out!r}")
check("заняло больше старого бюджета в 3 с (иначе тест ничего не доказывает)",
      elapsed > 3.0, f"elapsed={elapsed:.1f}s")
rx.terminate()
rx.communicate(timeout=10)

print("=== 2. собеседника нет, heartbeat жив (--ping 1) — сдаётся по молчанию, не виснет ===")
slot2 = os.path.join(tempfile.mkdtemp(prefix="rdpconn_wait2_"), "slot.txt")
t0 = time.monotonic()
out, err, code = run(
    ["rdpconn.tx", "--slot", f"file:{slot2}",
     "--peer-id", "A", "--ping", "1", "echo nikogo_net"],
    timeout=20,
)
elapsed = time.monotonic() - t0
check("tx завершился с ошибкой, не зависнув", code != 0, f"code={code} elapsed={elapsed:.1f}s")
check("уложился в разумное время (порог heartbeat 1x3=3 с + запас)",
      elapsed < 15.0, f"elapsed={elapsed:.1f}s")
check("пожаловался на молчание собеседника", "молчит" in out or "молчит" in err,
      f"out={out!r} err={err!r}")

print("=== 3. heartbeat выключен (--ping 0) — старый короткий бюджет без изменений ===")
slot3 = os.path.join(tempfile.mkdtemp(prefix="rdpconn_wait3_"), "slot.txt")
t0 = time.monotonic()
out, err, code = run(
    ["rdpconn.tx", "--slot", f"file:{slot3}",
     "--peer-id", "A", "--ping", "0", "--wait", "0.3", "--retries", "2", "echo nikogo"],
    timeout=15,
)
elapsed = time.monotonic() - t0
check("tx завершился с ошибкой", code != 0, f"code={code}")
check("коротко, как раньше (0.3 с x 2 плюс запас)", elapsed < 3.0, f"elapsed={elapsed:.1f}s")
check("тишина сообщена", "тишина" in out or "тишина" in err, f"out={out!r} err={err!r}")

print()
print("RESULT: ALL GREEN" if not _failed else f"RESULT: FAIL ({_failed})")
sys.exit(1 if _failed else 0)
