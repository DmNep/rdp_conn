"""Какие символы редирект буфера довозит без подмены.

Шлюз в этот момент не запускают: слот один. Сначала guest, потом host.
Формат кадра и префикс RDP1 здесь не используются.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdpconn.clipboard import ClipboardError, WindowsClipboard  # noqa: E402


def alphabet(name: str, n: int) -> str:
    if name == "ascii":
        return "".join(chr(0x20 + (i % 95)) for i in range(n))
    if name == "latin":
        return "".join(chr(0x00A1 + (i % 94)) for i in range(n))
    if name == "cyr":
        return "".join(chr(0x0410 + (i % 64)) for i in range(n))
    if name == "cjk":
        return "".join(chr(0x4E00 + (i % 256)) for i in range(n))
    if name == "fullwidth":
        return "".join(chr(0xFF01 + (i % 94)) for i in range(n))
    raise ValueError(name)


def pack(role: str, ident: int, kind: str, payload: str) -> str:
    return f"PROBE1\nROLE {role}\nID {ident}\nKIND {kind}\nN {len(payload)}\n{payload}"


def parse(text: str) -> dict | None:
    if not text or not text.startswith("PROBE1"):
        return None
    header: dict[str, str] = {}
    index = 0
    while index < len(text):
        newline = text.find("\n", index)
        if newline < 0:
            return None
        line = text[index:newline].rstrip("\r")
        index = newline + 1
        if line.startswith("N "):
            try:
                count = int(line.split()[1])
            except (IndexError, ValueError):
                return None
            header["n"] = str(count)
            header["payload"] = text[index:index + count]
            return header
        if " " in line:
            key, value = line.split(" ", 1)
            header[key.lower()] = value
    return None


def noise_cjk(n: int) -> str:
    """Последовательность без периода: поток sha256, отображённый в 256 иероглифов."""
    raw = bytearray()
    index = 0
    while len(raw) < n:
        raw += hashlib.sha256(index.to_bytes(4, "little")).digest()
        index += 1
    return "".join(chr(0x4E00 + byte) for byte in raw[:n])


def digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-16-le")).hexdigest()


def first_diff(left: str, right: str) -> tuple[int, str]:
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            return index, f"U+{ord(right[index]):04X}"
    if len(left) != len(right):
        return limit, f"len {len(right)}"
    return -1, "-"


class Slot:
    def __init__(self) -> None:
        self.store = WindowsClipboard()
        self.seen = self.store.seq()
        try:
            self.last = self.store.read() or ""
        except ClipboardError:
            self.last = ""
        # Первая запись сразу после чужого снимка часто не уезжает.
        self.repeat_next = False

    def write(self, text: str) -> None:
        self.store.write(text)
        self.seen = self.store.seq()
        self.last = text

    def changed(self, timeout: float) -> str | None:
        """Номер слота приходит раньше текста. Пока строка старая, номер не закрываем."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                seq = self.store.seq()
            except ClipboardError:
                time.sleep(0.05)
                continue
            if seq == self.seen:
                time.sleep(0.05)
                continue
            try:
                text = self.store.read() or ""
            except ClipboardError:
                time.sleep(0.05)
                continue
            if text != self.last:
                self.last = text
                self.seen = seq
                return text
            time.sleep(0.05)
        return None

    def announce(self, text: str, pause: float = 0.94) -> None:
        """Одна запись. Повтор ответа стирает следующий запрос."""
        del pause
        self.write(text)

    def wait(self, role: str, ident: int, timeout: float) -> dict | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            text = self.changed(max(0.1, end - time.monotonic()))
            if text is None:
                return None
            msg = parse(text)
            if msg and msg.get("role") == role and msg.get("id") == str(ident):
                return msg
        return None


def expect_sample(kind: str) -> str | None:
    parts = kind.split()
    if len(parts) < 2 or parts[0] != "sample":
        return None
    name = parts[1]
    if name == "newline":
        return sample_range("newline")
    if len(parts) >= 3:
        return alphabet(name, int(parts[2]))
    return sample_range(name)


def local_verdict(kind: str, payload: str) -> str:
    want = expect_sample(kind)
    if want is None:
        return f"n {len(payload)}"
    if payload == want:
        return f"ДА {len(payload)} sha {digest(payload)[:12]}"
    index, got = first_diff(want, payload)
    return (
        f"НЕТ len {len(payload)} из {len(want)} "
        f"с позиции {index} увидели {got} sha {digest(payload)[:12]}"
    )


def reply_for(msg: dict) -> str:
    payload = msg.get("payload", "")
    kind = msg.get("kind", "")
    ident = int(msg["id"])
    if kind.startswith("ask "):
        _ask, alpha, count_s = kind.split()
        count = int(count_s)
        body = noise_cjk(count) if alpha == "noise" else alphabet(alpha, count)
        return pack("guest", ident, f"data {alpha}", body)
    # Гость видит уже доехавший текст: хеш и длина — это то, что реально пришло.
    extra = f"{len(payload)}\n{digest(payload)}\n-1\n-"
    return pack("guest", ident, "saw", extra)


def serve(report: Path) -> int:
    slot = Slot()
    lines = ["guest started"]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    slot.write(pack("guest", 0, "ready", "ok"))
    lines.append("ready sent")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("guest ждёт пробы", flush=True)
    answered: set[str] = set()
    while True:
        found = None
        end = time.monotonic() + 180
        while time.monotonic() < end:
            text = slot.changed(max(0.1, end - time.monotonic()))
            if text is None:
                break
            lines.append(f"увидел {len(text)} символов {text[:80]!r}")
            report.write_text("\n".join(lines) + "\n", encoding="utf-8")
            parsed = parse(text)
            if parsed and parsed.get("role") == "host":
                ident = parsed.get("id", "")
                if ident in answered:
                    lines.append(f"повтор id {ident}, без второго ответа")
                    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    continue
                found = parsed
                break
        if found is None:
            lines.append("таймаут")
            break
        kind = found.get("kind", "")
        payload = found.get("payload", "")
        lines.append(
            f"id {found.get('id')} kind {kind} {local_verdict(kind, payload)}"
        )
        print(lines[-1], flush=True)
        answered.add(str(found.get("id")))
        if kind == "stop":
            slot.write(pack("guest", int(found["id"]), "stop", "ok"))
            break
        slot.announce(reply_for(found))
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


def exchange(slot: Slot, ident: int, kind: str, payload: str, timeout: float) -> dict | None:
    text = pack("host", ident, kind, payload)
    slot.write(text)
    if slot.repeat_next:
        # Этот снимок написан сразу после чужого. Один повтор другим хвостом,
        # затем тишина: серия стёрла бы ответ.
        early = slot.wait("guest", ident, 0.94)
        if early is not None:
            slot.repeat_next = True
            return early
        try:
            still_ours = slot.store.seq() == slot.seen
        except ClipboardError:
            still_ours = False
        if still_ours:
            slot.write(text + "\n.")
    msg = slot.wait("guest", ident, timeout)
    slot.repeat_next = msg is not None
    return msg


def sample_range(name: str) -> str:
    ranges = {
        "ascii": "".join(chr(code) for code in range(0x20, 0x7F)),
        "latin": "".join(chr(code) for code in range(0x00A0, 0x0100)),
        "cyr": "".join(chr(code) for code in range(0x0400, 0x0460)),
        "cjk": "".join(chr(code) for code in range(0x4E00, 0x4F00)),
        "fullwidth": "".join(chr(code) for code in range(0xFF01, 0xFF5F)),
        "newline": "A\nB\rC\tD",
    }
    return ranges[name]


def judge_saw(payload: str, msg: dict | None) -> str:
    if msg is None:
        return "НЕТ ОТВЕТА"
    body = msg.get("payload", "")
    parts = body.split("\n")
    if len(parts) < 2:
        return f"ОТВЕТ КОРОТКИЙ {body!r}"
    got_n = int(parts[0])
    got_sha = parts[1]
    want = digest(payload)
    if got_n == len(payload) and got_sha == want:
        return f"ДА {got_n} символов"
    return f"НЕТ пришло {got_n} из {len(payload)} sha {got_sha[:8]}"


def judge_data(alpha: str, count: int, msg: dict | None) -> str:
    if msg is None:
        return "НЕТ ОТВЕТА"
    payload = msg.get("payload", "")
    want = alphabet(alpha, count)
    if payload == want:
        return f"ДА {len(payload)} символов"
    index, got = first_diff(want, payload)
    return f"НЕТ len {len(payload)} из {count} с позиции {index} увидели {got}"


def judge_noise(count: int, msg: dict | None) -> str:
    if msg is None:
        return "НЕТ ОТВЕТА"
    payload = msg.get("payload", "")
    want = noise_cjk(count)
    if payload == want:
        return f"ДА {len(payload)} символов"
    index, got = first_diff(want, payload)
    return f"НЕТ len {len(payload)} из {count} с позиции {index} увидели {got}"


def drive(report: Path, quick: bool = False, low: int = 1000,
          high: int = 24000, steps: int = 8, noise: bool = False) -> int:
    slot = Slot()
    lines = ["алфавит буфера, живой редирект"]
    if parse(slot.last) and parse(slot.last).get("role") == "guest" and parse(slot.last).get("id") == "0":
        ready = True
    else:
        ready = slot.wait("guest", 0, 15) is not None
    lines.append("гость готов" if ready else "гость не готов")
    print(lines[-1], flush=True)
    if ready:
        slot.repeat_next = True
    ident = 1
    if not noise:
        print("образцы символов", flush=True)
        names = ("ascii", "newline") if quick else ("ascii", "latin", "cyr", "cjk", "fullwidth", "newline")
        for name in names:
            payload = sample_range(name)
            msg = exchange(slot, ident, f"sample {name}", payload, 12)
            line = f"хост → гость  {name:10}  {judge_saw(payload, msg)}"
            lines.append(line)
            print(line, flush=True)
            ident += 1
    if noise:
        print("несжимаемый снимок", flush=True)
        # Первый ответ после готовности часто не возвращается. Прогрев не входит в отчёт.
        exchange(slot, ident, "sample ascii", sample_range("ascii"), 8)
        ident += 1
        for direction, prefix in (("хост → гость", "blob"), ("гость → хост", "ask")):
            best = 0
            sizes = (8000, 12000, 14000, 16000) if prefix == "blob" else (16000, 28000, 48000)
            for count in sizes:
                if prefix == "blob":
                    payload = noise_cjk(count)
                    msg = exchange(slot, ident, f"blob {count}", payload, 20)
                    verdict = judge_saw(payload, msg)
                else:
                    msg = exchange(slot, ident, f"ask noise {count}", "", 25)
                    verdict = judge_noise(count, msg)
                line = f"{direction}  несжимаемые {count} символов  {verdict}"
                lines.append(line)
                print(line, flush=True)
                ident += 1
                if not verdict.startswith("ДА"):
                    break
                best = count
            lines.append(f"{direction}  наибольший целый несжимаемый снимок {best} символов")
    else:
        print("размер снимка", flush=True)
        for direction, prefix in (("хост → гость", "sample"), ("гость → хост", "ask")):
            low_n = 1000
            high_n = 2000 if quick else high
            best = 0
            nsteps = 1 if quick else steps
            for _ in range(nsteps):
                count = (low_n + high_n) // 2
                if prefix == "sample":
                    payload = alphabet("cjk", count)
                    msg = exchange(slot, ident, f"sample cjk {count}", payload, 15)
                    ok = judge_saw(payload, msg).startswith("ДА")
                else:
                    msg = exchange(slot, ident, f"ask cjk {count}", "", 15)
                    ok = judge_data("cjk", count, msg).startswith("ДА")
                mark = "ДА" if ok else "НЕТ"
                line = f"{direction}  cjk {count} символов  {mark}"
                lines.append(line)
                print(line, flush=True)
                if ok:
                    best = count
                    low_n = count + 1
                else:
                    high_n = count - 1
                ident += 1
                if low_n > high_n:
                    break
            lines.append(f"{direction}  наибольший целый cjk-снимок {best} символов")
            if best:
                b64 = ((best + 2) // 3) * 4
                lines.append(
                    f"{direction}  {best} символов cjk = {best} байт; "
                    f"base64 тех же байт занял бы {b64} символов"
                )
    msg = exchange(slot, ident, "stop", "", 8)
    lines.append("стоп " + ("ДА" if msg else "НЕТ"))
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(report, flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("host", "guest"))
    parser.add_argument("--report", required=True)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--high", type=int, default=24000)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--noise", action="store_true")
    args = parser.parse_args()
    path = Path(args.report)
    if args.mode == "guest":
        return serve(path)
    return drive(path, quick=args.quick, high=args.high, steps=args.steps, noise=args.noise)


if __name__ == "__main__":
    sys.exit(main())
