"""Рукопожатие F1 (fileshake.py): offer/ready/done/fail, слот в памяти.

Только протокол текстового рукопожатия — реальный fileclip.send_files()/
recv_file() сюда не входят (COM/shell32, нужна Windows и для FileContents
нужна настоящая RDP-передача, см. fileclip.py). Offer() трогает диск только
для os.path.getsize(), поэтому файлы для теста — пустые временные.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdpconn import clipboard as cb
from rdpconn.fileshake import Handshake, HandshakeError, decode, encode

fails = []


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def pair():
    cb.MemoryClipboard.reset()
    a = Handshake(cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A", auto_pause=False))
    b = Handshake(cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B", auto_pause=False))
    return a, b


def make_files(sizes: list[int]) -> list[str]:
    d = tempfile.mkdtemp(prefix="fileshake_test_")
    paths = []
    for i, n in enumerate(sizes):
        p = os.path.join(d, f"f{i}.bin")
        with open(p, "wb") as f:
            f.write(os.urandom(n))
        paths.append(p)
    return paths


print("=== 1. encode/decode F1 ===")
raw = encode(1, b"hello", b"NNNN")
parsed = decode(raw)
check("op, nonce и тело совпали", parsed == (1, b"NNNN", b"hello"), repr(parsed))
check("чужая магия не разбирается", decode(b"XXhello12345") is None)
check("короткий payload не разбирается", decode(b"F") is None)

print("=== 1b. одинаковый payload, разный nonce -> разные байты кадра и разный nonce в decode() ===")
same_a = encode(1, b"same body", b"aaaa")
same_b = encode(1, b"same body", b"bbbb")
check("nonce меняет байты кадра", same_a != same_b, (same_a, same_b))
dec_a, dec_b = decode(same_a), decode(same_b)
check("op и тело совпали несмотря на разный nonce",
     dec_a[0] == dec_b[0] == 1 and dec_a[2] == dec_b[2] == b"same body", (dec_a, dec_b))
check("а вот nonce в decode() — разный (на нём и держится проверка в wait_result()/offer())",
     dec_a[1] != dec_b[1], (dec_a[1], dec_b[1]))

print("=== 2. offer -> accept -> ready ===")
sender, receiver = pair()
paths = make_files([100, 200])
result = {}


def do_accept():
    offer = receiver.wait_offer(5.0)
    result["offer"] = offer
    if offer is not None:
        receiver.accept()


thread = threading.Thread(target=do_accept)
thread.start()
try:
    offer = sender.offer(paths, chunk=65536, ready_timeout=5.0)
    result["sent_ok"] = True
except HandshakeError as exc:
    result["sent_ok"] = False
    result["err"] = str(exc)
thread.join(5)
check("offer() не бросил", result.get("sent_ok"), result.get("err", ""))
check("получатель увидел offer", result.get("offer") is not None)
if result.get("offer") is not None:
    got = result["offer"]
    names = sorted(Path(p).name for p in paths)
    check("имена файлов совпали", sorted(n for n, _ in got.files) == names, got.files)
    check("размеры файлов совпали", sorted(s for _, s in got.files) == sorted([100, 200]))
    check("чанк дошёл", got.chunk == 65536, got.chunk)

print("=== 3. offer -> decline -> отказ виден отправителю ===")
sender, receiver = pair()
paths = make_files([10])


def do_decline():
    offer = receiver.wait_offer(5.0)
    if offer is not None:
        receiver.decline("занят другим")


thread = threading.Thread(target=do_decline)
thread.start()
try:
    sender.offer(paths, ready_timeout=5.0)
    check("decline приводит к отказу", False)
except HandshakeError as exc:
    check("decline приводит к отказу", "занят другим" in str(exc), str(exc))
thread.join(5)

print("=== 4. offer без ответа — таймаут ===")
sender, _receiver = pair()
paths = make_files([10])
try:
    sender.offer(paths, ready_timeout=0.3)
    check("таймаут offer()", False)
except HandshakeError as exc:
    check("таймаут offer()", "READY" in str(exc), str(exc))

print("=== 5. report_done -> wait_result (через настоящий offer/accept, не в обход) ===")
sender, receiver = pair()
paths5 = make_files([100, 200])
expected = [(Path(paths5[0]).name, 100, "aa" * 32), (Path(paths5[1]).name, 200, "bb" * 32)]


def do_report():
    offer = receiver.wait_offer(5.0)
    if offer is not None:
        receiver.accept()
        receiver.report_done(expected)


thread = threading.Thread(target=do_report)
thread.start()
try:
    sender.offer(paths5, ready_timeout=5.0)
    got = sender.wait_result(5.0)
    check("результат совпал", got == expected, got)
except HandshakeError as exc:
    check("результат совпал", False, str(exc))
thread.join(5)

print("=== 6. report_fail -> wait_result бросает с причиной (через настоящий offer/accept) ===")
sender, receiver = pair()
paths6 = make_files([10])


def do_fail():
    offer = receiver.wait_offer(5.0)
    if offer is not None:
        receiver.accept()
        receiver.report_fail("буфер побило")


thread = threading.Thread(target=do_fail)
thread.start()
try:
    sender.offer(paths6, ready_timeout=5.0)
    sender.wait_result(5.0)
    check("report_fail виден отправителю", False)
except HandshakeError as exc:
    check("report_fail виден отправителю", "буфер побило" in str(exc), str(exc))
thread.join(5)

print("=== 6b. wait_result игнорирует чужой/старый OP_DONE с другим nonce ===")
sender, receiver = pair()
# Та же сторона (peer_id "B", как у receiver), но другой Handshake и свой
# nonce — имитирует кадр, оставшийся в истории слота от не связанной
# попытки. transport общий с receiver (тот же peer_id), поэтому для
# sender.wait_result() это "чужой", не "свой", кадр — как и было бы на
# живом стенде.
stale_hs = Handshake(receiver.transport)
stale_hs.report_done([("stale.bin", 1, "00" * 32)])  # кадр с OP_DONE уже лежит в слоте


def do_report_7():
    offer = receiver.wait_offer(5.0)
    if offer is not None:
        receiver.accept()
        receiver.report_done(expected)


paths7 = make_files([100, 200])
thread = threading.Thread(target=do_report_7)
thread.start()
try:
    sender.offer(paths7, ready_timeout=5.0)
    got = sender.wait_result(5.0)
    check("настоящий результат дошёл, а не чужой устаревший OP_DONE",
         got == expected, got)
except HandshakeError as exc:
    check("настоящий результат дошёл, а не чужой устаревший OP_DONE", False, str(exc))
thread.join(5)

print("=== 7. wait_result без ответа — таймаут ===")
sender, _receiver = pair()
try:
    sender.wait_result(0.3)
    check("таймаут wait_result()", False)
except HandshakeError as exc:
    check("таймаут wait_result()", "DONE" in str(exc), str(exc))

print("=== 8. слишком большой offer отвергается локально, без отправки ===")
sender, receiver = pair()
many = make_files([1] * 20)
try:
    sender.offer(many, ready_timeout=0.3)
    check("переполненный offer отвергнут", False)
except HandshakeError as exc:
    check("переполненный offer отвергнут", "не влезает" in str(exc), str(exc))
# Ничего не должно было уйти в буфер — приёмник не видит кадра совсем.
raw = receiver.transport.receive(0.2)
check("в буфере пусто", raw is None, raw)

print()
if fails:
    print(f"RESULT: FAIL {len(fails)}")
    sys.exit(1)
print("RESULT: ALL GREEN")
