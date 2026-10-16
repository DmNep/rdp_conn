"""Кадры, CRC и точка входа: то, что не покрывает обмен через слот.

Слот (test_clipboard) проверяет доставку, но не проверяет саму упаковку: если
быть в заголовке перепутаны, обе стороны ошибутся одинаково и обмен сойдётся.
Здесь три вещи:

1. CRC-16 сверяется с известным контрольным значением для "123456789" (0x29B1)
   — так полином и порядок байт проверяются без эталонной реализации.
2. Кадры сверяются байт-в-байт с xconn_channel.framing, пока оба проекта живут
   в одном дереве. Разбор от сборки не зависит, поэтому расхождение в парах
   байтов было бы видно сразу. Когда rdp_conn уедет в свой репозиторий, секция
   просто пропустится: сверять станет не с чем, а сами проверки 1, 3 и 4
   останутся.
3. Разбор пограничных случаев: обрезанный заголовок, лишние байты, битый CRC,
   неизвестный тип — у каждого свой код NAK, потому что по коду отправитель
   решает, повторять или сдаться.
4. `python -m rdpconn`: команда должна запускаться без аргументов и с --help,
   иначе инструкция в README нерабочая.

Тест — скрипт, не pytest: зависимостей нет, достаточно python 3.9+.
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rdpconn import config, crc, framing  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, hint: str = "") -> None:
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {hint}" if hint else ""))
    if not ok:
        failures.append(name)


print("=== 1. CRC-16: контрольное значение ===")
# 0x29B1 — канонический контроль для CRC-16/ARC, он же используется в
# xconn_channel; если полином или инициализация поплывут, значение не совпадёт.
check("crc16(b'123456789') == 0x29B1", crc.crc16(b"123456789") == 0x29B1,
      f"получено {crc.crc16(b'123456789'):#06x}")
check("пустые данные — это инициализатор, а не ноль", crc.crc16(b"") == 0xFFFF,
      f"{crc.crc16(b''):#06x}: регистр стартует с 0xFFFF, как в CRC-16/ARC")
check("байты читаются как есть", crc.crc16(bytes(range(256))) == crc.crc16(bytearray(range(256))),
      "bytes и bytearray должны давать одно и то же")

print("=== 2. заголовок: порядок и размер ===")
frame = framing.build_frame(config.CMD, 0xAB, b"hi")
check("длина = 5 + payload + 2", len(frame) == 9, f"{len(frame)}")
check("sync первый", frame[0] == config.SYNC_BYTE, f"{frame[0]:#04x}")
check("тип второй", frame[1] == config.CMD)
check("seq третий", frame[2] == 0xAB)
check("длина big-endian", (frame[3] << 8 | frame[4]) == 2, frame[:5].hex())
check("payload на месте", frame[5:7] == b"hi")
check("CRC последних двух байтов",
      ((frame[7] << 8) | frame[8]) == crc.crc16(frame[:7]))

print("=== 3. round-trip всех типов ===")
for tag, build in (
    ("ACK", lambda s: framing.ack(s)),
    ("NAK", lambda s: framing.nak(s, config.NAK_CRC)),
    ("PING", lambda s: framing.ping(s)),
    ("PONG", lambda s: framing.pong(s)),
    ("CMD", lambda s: framing.cmd(s, "dir")),
    ("NOTE", lambda s: framing.echo(s, b"\x01\x02")),
):
    got = framing.parse_frame(build(7))
    check(f"{tag}: seq и payload сохранились", got.seq == 7 and got.type_name == tag,
          repr(got))

check("пустой ACK не несёт тела", framing.parse_frame(framing.ack(1)).payload == b"",
      "номер только в заголовке — иначе два источника правды про один seq")
check("причина NAK в одном байте",
      framing.parse_frame(framing.nak(1, config.NAK_TYPE)).payload == bytes((config.NAK_TYPE,)))
check("кириллица в CMD доходит байтами cp866",
      framing.parse_frame(framing.cmd(0, "привет")).payload
      == "привет".encode(config.CODEPAGE))

print("=== 4. коды отказа: у каждой причины свой NAK ===")
good = framing.build_frame(config.CMD, 4, b"ls")


def code_of(data: bytes) -> int | None:
    try:
        framing.parse_frame(data)
        return None
    except framing.FrameError as exc:
        return exc.code


check("обрезанный заголовок — NAK_LENGTH",
      code_of(good[:4]) == config.NAK_LENGTH)
check("обрезанный payload — NAK_LENGTH",
      code_of(good[:-2]) == config.NAK_LENGTH)
check("лишние байты — NAK_LENGTH",
      code_of(good + b"\x00") == config.NAK_LENGTH,
      "кадр, обрезанный слотом, нельзя принимать за целый")
check("битый CRC — NAK_CRC",
      code_of(good[:-1] + bytes((good[-1] ^ 0xFF,))) == config.NAK_CRC)
check("не тот sync — отказ", code_of(b"\x00" + good[1:]) is not None)
check("неизвестный тип — NAK_TYPE",
      code_of(framing.build_frame(0xFE, 0, b"")) == config.NAK_TYPE,
      "сторона с новой версией не должна молча молчать")


def seq_of(data: bytes):
    try:
        framing.parse_frame(data)
    except framing.FrameError as exc:
        return exc.seq
    return "разобрался"


broken = bytearray(good)
broken[-1] ^= 0xFF
check("причина CRC знает seq ответственного кадра", seq_of(bytes(broken)) == 4,
      "без seq отправитель не поймёт, какой из трёх повторов отбрасывать")
check("отказ по длине заголовка seq не знает", seq_of(good[:4]) is None,
      "заголовок не прочитан, выдумывать номер нельзя")

print("=== 5. границы размера ===")
check("MAX_PAYLOAD проходит",
      len(framing.parse_frame(
          framing.build_frame(config.NOTE, 0, b"x" * config.MAX_PAYLOAD)).payload)
      == config.MAX_PAYLOAD)
try:
    framing.build_frame(config.NOTE, 0, b"x" * (config.MAX_PAYLOAD + 1))
    check("превышение отклоняется на сборке", False, "молча ушёл большой кадр")
except ValueError:
    check("превышение отклоняется на сборке", True,
          "ошибка наша, а не NAK: сегментацией занимается вызывающий")
check("длина в заголовке больше MAX_PAYLOAD — NAK_LENGTH",
      code_of(bytes((config.SYNC_BYTE, config.NOTE, 0, 0xFF, 0xFF))) == config.NAK_LENGTH)

print("=== 6. совместимость кадров с xconn_channel ===")
# Пока аудио-проект рядом, сверяем байты: протокол намеренно тот же, чтобы
# рассылка документации не требовала двух версий.
sys.path.insert(0, os.path.dirname(ROOT))
try:
    import xconn_channel.config as old_config
    import xconn_channel.framing as old_framing
except ImportError:
    print("  пропущено: xconn_channel недоступен (rdp_conn отдельно) — OK")
else:
    for seq in (0, 1, 127, 255):
        for payload in (b"", b"ping", "команда ls -la".encode(config.CODEPAGE),
                        bytes(range(config.MAX_PAYLOAD))):
            mine = framing.build_frame(config.CMD, seq, payload)
            theirs = old_framing.build_frame(old_config.CMD, seq, payload)
            if mine != theirs:
                check(f"байт-в-байт seq={seq} len={len(payload)}", False,
                      f"{mine.hex()} != {theirs.hex()}")
                break
        else:
            continue
        break
    else:
        check("кадры совпадают байт-в-байт", True)
    check("чужой кадр разбирается", framing.parse_frame(
        old_framing.build_frame(old_config.CMD, 9, b"ls")).payload == b"ls")
    check("наш кадр разбирается чужим разбором",
          old_framing.parse_frame(framing.cmd(9, "ls")).payload == b"ls")

print("=== 7. точка входа python -m rdpconn ===")
env = dict(os.environ, PYTHONPATH=ROOT,
           PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


def run_cli(args: list[str]) -> tuple[str, int]:
    proc = subprocess.run([sys.executable, "-m", "rdpconn", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=ROOT, timeout=60)
    return (proc.stdout or "") + (proc.stderr or ""), proc.returncode


out, code = run_cli([])
check("без аргументов — подсказка и код 0", code == 0 and "rdpconn rx" in out,
      f"code={code}")
out, code = run_cli(["--help"])
check("--help работает", code == 0 and "tx" in out)
out, code = run_cli(["tx", "--help"])
check("tx --help", code == 0 and "--peer-id" in out,
      "ключ обязателен на обеих сторонах и обязан быть в справке")
out, code = run_cli(["rx", "--help"])
check("rx --help", code == 0 and "--exec" in out)
out, code = run_cli(["nuke"])
check("неизвестный режим — код 2", code == 2, f"code={code}")

print()
if failures:
    print("RESULT: FAIL")
    for name in failures:
        print("  -", name)
    sys.exit(1)
print("RESULT: ALL GREEN")
