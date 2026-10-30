"""Замер скорости канала: сколько кадров и байт в секунду он реально несёт.

Зачем слоями. Один замер «сколько шла команда» смешивает три вещи разной
природы: Python-разбор, паузу опроса (POLL_S = 0.05) и сам транспорт. На стенде
с RDP-редиректом доминирует транспорт, на одной машине — опрос, и вывод из
смешанного замера был бы неверен для боя. Поэтому слои разделены:

  cpu  — сборка/разбор кадра и текста слота (framing + base64 + merge);
  poll — нижняя граница того же через send()/receive(): ожидание не наступает;
  rtt  — задержка одного шага без процессов: цена транспорта плюс POLL_S;
  proc — два процесса через file:PATH: честный RTT и пропускная способность;
  win  — то же через настоящий буфер обмена этой машины (--win);
  remote — бой: rx на второй машине, единственный слой, видящий редирект RDP.

Полезной нагрузкой считается payload кадра CMD: кадр на 7 байт длиннее, а в
слоте он ещё и в base64, поэтому байт/с приводится и по payload, и по кадру.

    python tools/bench_speed.py                       # cpu + poll + rtt + proc
    python tools/bench_speed.py --frames 100          # меньше кадров в cpu/poll
    python tools/bench_speed.py --proc-frames 80      # больше кадров в proc
    python tools/bench_speed.py --win                 # добавить боевой слот
    python tools/bench_speed.py --payload 40          # короткие команды вместо 240
    python tools/bench_speed.py --rtt-only            # только задержка, win-слот
    python tools/bench_speed.py --rtt-only --poll 0.01  # что даст более частый опрос
    python tools/bench_speed.py --remote --proc-frames 20  # замер в бою

Режим --remote поднят отдельно, потому что на стенде он единственный годный:
редирект буфера живёт между двумя машинами, и подменить его локальным слотом
нельзя. Остальные слои на бою дают оптимистичную цифру локального буфера.

Скрипт, не pytest: зависимостей нет, как и в остальных проверках.
"""
from __future__ import annotations

import argparse
import base64
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from queue import Empty, Queue

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rdpconn import clipboard as clipboard_mod  # noqa: E402
from rdpconn import config, framing  # noqa: E402
from rdpconn.clipboard import (  # noqa: E402
    POLL_S,
    SNAPSHOT_MAX,
    ClipboardTransport,
    MemoryClipboard,
    open_store,
    snapshot_pieces,
)

PY = sys.executable
ENV = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")


def pct(values, share: float) -> float:
    """Перцентиль на маленькой выборке без numpy: сортировка и индекс."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))
    return ordered[index]


def rate(seconds: float, frames: int, payload: int) -> str:
    """Кадры/с, payload-байт/с и KiB/с в одну строку."""
    if seconds <= 0 or frames <= 0:
        return "нет данных"
    fps = frames / seconds
    bps = fps * payload
    return (f"{fps:8.1f} кадр/с  payload {bps:8.0f} Б/с = {bps / 1024:6.2f} КиБ/с"
            f"  кадр {fps * (payload + 7):8.0f} Б/с")


# --- слой 1: только Python, без ожидания ------------------------------------


def bench_cpu(frames: int, payload_size: int) -> None:
    print("=== cpu: сборка + base64 + merge + разбор (без ожидания) ===")
    store = MemoryClipboard()
    tx = ClipboardTransport(store, peer_id="A")
    rx = ClipboardTransport(store, peer_id="B")
    payload = b"x" * payload_size
    frame = framing.build_frame(config.CMD, 0, payload)

    # Прогрев: первый проход платит за импорты base64 и прогрев кешей ctypes.
    for _ in range(20):
        tx._publish(frame)
        rx._accept(store.read())

    started = time.perf_counter()
    delivered = 0
    for _ in range(frames):
        tx._publish(frame)
        for raw in rx._accept(store.read()):
            framing.parse_frame(raw)
            delivered += 1
    spent = time.perf_counter() - started

    slot_text = store.read() or ""
    b64_len = len(base64.b64encode(frame))
    print(f"  кадров: {delivered} за {spent:.3f} с -> "
          f"{delivered / spent:1.0f} кадр/с, {spent / max(1, delivered) * 1e6:.0f} мкс на кадр")
    print("  " + rate(spent, delivered, payload_size))
    # Overhead транспорта виден именно здесь: он не зависит от задержек и
    # поэтому переносится на боевой стенд байт в байт.
    print(f"  кадр: {len(frame)} Б -> {b64_len} Б в слоте "
          f"(+{b64_len - len(frame)} Б, или {b64_len / len(frame):.2f}x) — "
          "в RDP идёт именно это")
    print(f"  текст слота: {len(slot_text)} символов, ячеек: {len(rx._parse(slot_text))}")
    print(f"  полезного в снимке слота: {payload_size / max(1, len(slot_text)):.0%} "
          "(остальное — маркер, история и base64)")


# --- слой 2а: RTT без процессов ---------------------------------------------


def bench_rtt(slot: str, rounds: int, payload_size: int,
              poll: float | None = None) -> float:
    """Задержка одного шага на выбранном слоте без дочерних процессов.

    Смысл слоя в том, что он меряет ровно транспорт и опрос. Слой процессов
    добавляет к шагу переключение между процессами и старт интерпретатора, и
    на боевом стенде, где шаг и так секунды, эта накладная доля тонет, а на
    одной машине — наоборот, съедает половину цифры. Здесь же остаётся то, что
    переносится на бой без изменений: цена записи в буфер и цена POLL_S.

    Получатель живёт в отдельном потоке, а не в том же, что отправитель. Иначе
    очередь буфера меняется ещё до вызова receive(), ожидание не наступает ни
    разу и замер даёт 0 мс — ровным счётом ничего про транспорт не говорящий
    (это и есть наблюдаемый результат слоя poll). Поток входит в receive()
    заранее, то есть засыпает на POLL_S по-настоящему, как на бою.

    Слой меняет POLL_S в модуле clipboard, а не в своём импорте: receive()
    читает константу из своего модуля, и подмена локального имени оставила бы
    канал с прежним опросом, а отчёт — с новым.

    Возвращает медиану шага в миллисекундах.
    """
    if poll is not None:
        clipboard_mod.POLL_S = poll
    poll_now = clipboard_mod.POLL_S
    print(f"=== rtt: один процесс, два конца на слоте {slot}, POLL_S={poll_now} ===")
    try:
        store = open_store(slot) if slot != "memory" else MemoryClipboard()
    except Exception as exc:  # слот может быть недоступен (не-Windows)
        print(f"  пропущен: {exc}")
        return 0.0

    tx = ClipboardTransport(store, peer_id="A")
    rx = ClipboardTransport(store, peer_id="B")
    text = "x" * payload_size

    def drain(side) -> None:
        """Выбросить кадры, оставшиеся с прошлого раунда.

        Подтверждение прошлого шага может приехать уже после того, как шаг
        засчитан, — на бою это штатный повтор. Оставшись в очереди, он был бы
        принят за ответ текущего раунда и дал бы ложные доли миллисекунды.
        """
        while side.receive(0.0) is not None:
            pass

    def one_round(seq: int):
        """Один шаг: CMD, приём и ACK в потоке, ожидание ACK у отправителя.

        Приёмник обязан успеть войти в receive() до отправки — иначе он не
        заснёт на POLL_S и шаг выйдет нулевым. Пауза перед отправкой много
        меньше POLL_S и сама по себе квантование не прячет.
        """
        drain(tx)
        drain(rx)
        result = {}

        def reader() -> None:
            started = time.perf_counter()
            got = rx.receive(5.0)
            if got is None:
                arrived.set()
                return
            result["one_way"] = time.perf_counter() - started
            # ACK пишется здесь же, как это делает rx.handle: подтверждение
            # обязано быть частью измеряемого шага, а не идти после замера —
            # иначе слой считал бы только половину обмена.
            rx.send(framing.ack(seq))

        arrived = threading.Event()
        listener = threading.Thread(target=reader, daemon=True)
        listener.start()
        time.sleep(min(0.002, poll_now / 10))
        started = time.perf_counter()
        tx.send(framing.cmd(seq, text))
        step, acked = None, False
        # Ждём именно ACK своего seq: чужой кадр (эхо, повтор прошлого раунда)
        # не должен завершать замер раньше времени.
        while time.perf_counter() - started < 5.0:
            got = tx.receive(0.05)
            if got is None:
                continue
            try:
                frame = framing.parse_frame(got)
            except framing.FrameError:
                continue
            if frame.type == config.ACK and frame.seq == seq:
                step = time.perf_counter() - started
                acked = True
                break
        listener.join(timeout=6.0)
        if not acked or "one_way" not in result:
            return None
        return step, result["one_way"]

    try:
        for i in range(3):  # прогрев: первый кадр платит за OpenClipboard
            one_round(i)
        steps, one_way = [], []
        for i in range(3, 3 + rounds):
            sample = one_round(i & 0xFF)
            if sample is None:
                continue
            steps.append(sample[0])
            one_way.append(sample[1])
    finally:
        tx.close()
        rx.close()

    if not steps:
        print("  FAIL: ни одного полного обмена")
        return 0.0
    median = statistics.median(steps)
    print(f"  обменов: {len(steps)}/{rounds}")
    print(f"  шаг CMD->ACK : медиана {median * 1000:6.1f} мс, "
          f"мин {min(steps) * 1000:6.1f}, p95 {pct(steps, 0.95) * 1000:6.1f} мс")
    print(f"  в одну сторону: медиана {statistics.median(one_way) * 1000:6.1f} мс, "
          f"мин {min(one_way) * 1000:6.1f} мс")
    print("  " + rate(sum(steps), len(steps), payload_size))
    print(f"  потолок при таком шаге: {config.MAX_PAYLOAD / median:5.1f} Б/с "
          f"({config.MAX_PAYLOAD / median / 1024:.2f} КиБ/с)")
    return median * 1000.0


# --- слой 2: обмен в одном процессе, с реальным receive() --------------------


def bench_poll(frames: int, payload_size: int) -> float:
    """Возвращает медиану шага в миллисекундах.

    Слой обязан называть себя правильно: оба конца крутятся в одном потоке,
    очередь буфера меняется до вызова receive(), ожидание не наступает ни разу
    и медиана выходит 0 мс. Это не поломка, а ровно то, что слой и меряет:
    чистую стоимость сборки, merge и разбора без всякого ожидания, — то же
    самое, что и cpu, но через публичный интерфейс транспорта. Завышать его до
    «реального RTT» нельзя, для задержки служит слой rtt.
    """
    print(f"=== poll: нижняя граница, ожидание не наступает (POLL_S={POLL_S}) ===")
    store = MemoryClipboard()
    tx = ClipboardTransport(store, peer_id="A")
    rx = ClipboardTransport(store, peer_id="B")
    text = "x" * payload_size
    rtts = []
    ok = 0
    for i in range(frames):
        seq = i & 0xFF
        started = time.perf_counter()
        tx.send(framing.cmd(seq, text))
        got = rx.receive(5.0)
        if got is None:
            continue
        framing.parse_frame(got)
        rx.send(framing.ack(seq))
        if tx.receive(5.0) is None:
            continue
        rtts.append(time.perf_counter() - started)
        ok += 1

    if not rtts:
        print("  FAIL: ни одного полного обмена CMD -> ACK")
        return 0.0
    total = sum(rtts)
    median = statistics.median(rtts)
    print(f"  обменов: {ok}/{frames}, шаг CMD->ACK за {total:.2f} с")
    print(f"  шаг: медиана {median * 1000:6.0f} мс, мин {min(rtts) * 1000:6.0f}, "
          f"p95 {pct(rtts, 0.95) * 1000:6.0f} мс")
    print("  " + rate(total, ok, payload_size))
    print("  (0 мс здесь — норма: см. докстринг; задержка с опросом — в слое rtt)")
    return median * 1000.0


# --- слой 5: бой, приёмник на второй машины ---------------------------------

# Сколько ждать подтверждения в бою. По умолчанию в канале --wait 1 с, и на
# редиректе это самообман: запись появляется у собеседника через 10-15 с
# (docs/protocol.md 5), tx объявит тишину раньше и уйдёт в ретрансляции. Обмен
# при этом выглядит живым, а замер даёт не скорость канала, а стоимость
# MAX_RETRY промахов по --wait.
REMOTE_WAIT_S = 25.0
# Ниже этого порога замер в бою бессмыслен: он меряет ретрансляции.
REMOTE_MIN_WAIT_S = 20.0


def clear_slot(slot: str) -> bool:
    """Освободить слот перед прогоном. True — запись пустого слота принята.

    Нужно именно в бою, и обнаружилось там же по косвенному признаку. Транспорт
    считает кадр новым по паре (номер, содержимое), а память о принятых кадрах
    живёт в процессе. Значит свежий tx заставит старые ячейки прошлого обмена
    выглядеть новыми: они лежат в том же тексте слота под теми же подписями, а
    запоминать их некому. Приёмник на это не жалуется, он их просто печатает, а
    отправитель видит чужой ACK вместо своего (`ожидался ACK seq=0, получен
    ACK seq=30`), тратит попытку и ретранслирует. Замер в таком прогоне
    измеряет разбор завалов, а не канал.

    Слот очищается до запуска tx, а не после: очистка поверх уже отправленного
    CMD затёрла бы сам кадр — ровно то, от чего защита и существует.

    Пишется пустая строка, а не текст без маркера: второе было бы принято
    собеседником за человеческую копию и подняло бы busy.

    Успех записи — ещё не чистый слот: человек мог скопировать своё между нашей
    записью и первой командой. Поэтому False означает лишь «буфер недоступен»,
    а достоверность прогона отвечает не флаг, а счётчик старых кадров.
    """
    try:
        store = open_store(slot) if slot != "memory" else MemoryClipboard()
    except Exception as exc:
        print(f"  !! слот {slot} не открыть, очистка невозможна: {exc}")
        return False
    try:
        store.write("")
    except Exception as exc:
        print(f"  !! слот не очистить ({exc}) — старый обмен будет мешать")
        return False
    return True


def peer_command(peer_b: str, slot: str, wait: float, echo: bool = False) -> str:
    """Готовая команда для второй машины — чтобы не забыть нужные флаги.

    Печатается целиком, а не «запустите rx как обычно»: половину флагов
    забывают, и каждая забытая стоит испорченного прогона. --no-echo здесь
    не украшение: с эхом каждый шаг платит ещё одним циклом редиректа.
    """
    return (f"python -m rdpconn.rx --peer-id {peer_b} --slot {slot} --ping 0 "
            f"{'' if echo else '--no-echo '}--wait {wait:g} --duration 900")


def bench_remote(slot: str, frames: int, payload_size: int, wait: float,
                 reply_wait: float, peer_a: str, peer_b: str,
                 echo: bool) -> float:
    """Замер в бою: rx работает на второй машине, отсюда только tx.

    Локальные слои для стенда непригодны по двум причинам, и обе наблюдались.

    1. Они поднимают rx рядом, то есть меряют локальный буфер, а не редирект:
       редирект живёт между двумя машинами, подменить его нечем.
    2. Они делят время на число строк `<< ACK`. В бою подтверждений больше, чем
       команд: на повтор с тем же seq приёмник отвечает закешированным ACK
       (docs/protocol.md 4), и деление на них занижает шаг ровно на величину
       ретрансляций — то есть льстит каналу тогда, когда он хуже всего.

    Здесь шаг считается построчно от `>> CMD seq=N` до `<< ACK` того же обмена,
    поэтому старт интерпретатора и поднятие слота в цифру не попадают, а
    ретрансляция видна как `попытка 2`, а не съедена усреднением.

    Вывод tx читает отдельный поток в очередь. `readline()` прямо в цикле
    повесил бы прогон навсегда ровно тогда, когда подтверждений нет — то есть
    при обрыве канала, ради проверки которого режим и нужен; таймаут при
    блокирующем чтении не работает. Поток отдаёт строку с дедлайном, и обрыв
    различим от ожидания.

    Возвращает медиану шага в секундах (0 — подтверждений нет).
    """
    print(f"=== remote: rx на второй машине, отсюда tx, {frames} команд "
          f"по {payload_size} Б ===")
    if wait < REMOTE_MIN_WAIT_S:
        print(f"  !! {wait:g} с меньше задержки редиректа (10-15 с): "
              "подтверждение придёт после объявления тишины, и замер измерит "
              f"ретрансляции. Берите >= {REMOTE_MIN_WAIT_S:g}")
    if echo:
        print("  .. rx с эхом: NOTE приходит после ACK, поэтому в шаг CMD->ACK "
              "оно не входит, но каждая команда платит ещё одним циклом "
              "редиректа и попадает в «время на команду»")
    else:
        print("  .. ожидается rx с --no-echo и --ping 0; после ACK ждать "
              "нечего, поэтому reply-wait 0")

    print("  на второй машине (B) должен работать:")
    print("    " + peer_command(peer_b, slot, wait, echo))
    print("  условия: ровно один rx, ничего не копировать в буфер во время "
          "прогона")

    channel = ["--slot", slot, "--peer-id", peer_a, "--ping", "0",
               "--wait", f"{wait:g}", "--retries", "2"]
    # Слот обязан быть пустым до старта tx, иначе старые ячейки прошлого обмена
    # будут съедены как новые (см. clear_slot) и первые команды уйдут в
    # ретрансляции. rx перезапускать после очистки незачем: он принимает по
    # паре (номер, содержимое), а пустой слот для него — просто тишина.
    clear_slot(slot)
    time.sleep(0.5)  # дать rx увидеть опустевший слот до первой команды
    tx = subprocess.Popen(
        [PY, "-u", "-m", "rdpconn.tx"] + channel
        + ["--repl", "--reply-wait", f"{reply_wait:g}"],
        cwd=ROOT, env=ENV, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )

    lines: Queue = Queue()

    def pump() -> None:
        for line in tx.stdout:
            lines.put(line.rstrip())
        lines.put(None)  # EOF: процесс умер, ждать нечего

    threading.Thread(target=pump, daemon=True).start()

    steps, retries, silences, notes, stale = [], 0, 0, 0, 0
    # Хвост вывода копится здесь, а не читается из tx.stdout в конце: поток уже
    # читает pump, и второй читатель того же буфера отнял бы у него строки —
    # при обрыве человек увидел бы пустой отчёт вместо причины.
    tail: deque = deque(maxlen=6)
    t_cmd = None
    started_at = None

    def observe(deadline: float):
        """Ждать ACK своего CMD. -> ('ack', сек) | ('end', 0) | (None, 0)."""
        nonlocal t_cmd, retries, silences, notes, stale
        while time.perf_counter() < deadline:
            try:
                line = lines.get(timeout=max(0.05, deadline - time.perf_counter()))
            except Empty:
                continue
            if line is None:
                return "end", 0.0
            tail.append(line)
            # Метка берётся из той же строки, что и время: два источника
            # правды о seq здесь уже стоили потери кадра (docs/protocol.md 5).
            # «попытка 2» стоит на той же строке, что и «>> CMD». Отдельная
            # ветка её не видела, и шаг считался от повтора, а не от первой
            # отправки: 25 с ожидания превращались в 0.1 с.
            if "попытка 2" in line or "попытка 3" in line:
                retries += 1
            if ">> CMD" in line and t_cmd is None:
                t_cmd = time.perf_counter()
            elif "тишина" in line:
                silences += 1
            elif "<< NOTE" in line:
                notes += 1
            elif "ожидался ACK" in line:
                stale += 1
            elif "<< ACK" in line and t_cmd is not None:
                step = time.perf_counter() - t_cmd
                t_cmd = None
                return "ack", step
        return None, 0.0

    try:
        for index in range(frames):
            # Прогрев не выделяем: в бою первый кадр платит за OpenClipboard
            # доли секунды на фоне секунд редиректа, а вычитать их — значит
            # снова подгонять цифру под желаемую.
            text = f"@{index} " + "x" * max(0, payload_size - 4)
            if started_at is None:
                started_at = time.perf_counter()
            tx.stdin.write(text + "\n")
            tx.stdin.flush()
            # Дедлайном больше, чем ждёт сам канал: таймаут замера обязан быть
            # хуже таймаута протокола, иначе прерывался бы прогон, а не ретрансляция.
            kind, step = observe(time.perf_counter() + wait * 2 + 10)
            if kind == "end":
                print("  tx завершился: слот недоступен, кончилось время rx "
                      "или буфер занят (см. строки выше)")
                break
            if kind is None:
                print(f"  команда {index}: подтверждения нет за "
                      f"{wait * 2 + 10:g} с — обрыв, прогон прерван")
                break
            steps.append(step)
            print(f"  {index + 1:>3}/{frames}  шаг {step:6.2f} с   "
                  f"{payload_size / step:6.0f} Б/с")
    finally:
        try:
            tx.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            tx.wait(timeout=30)
        except subprocess.TimeoutExpired:
            tx.kill()
            tx.wait(timeout=10)
        # Дочитываем очередь после смерти процесса: pump кладёт None в EOF,
        # и всё, что tx успел напечатать перед смертью (обычно и есть причина
        # обрыва), должно попасть в хвост, а не потеряться в очереди.
        while True:
            try:
                line = lines.get(timeout=1.0)
            except Empty:
                break
            if line is None:
                break
            tail.append(line)

    if not steps:
        print("  FAIL: ни одного подтверждённого обмена — канал не работает, "
              "замерять нечего")
        print(f"  повторов: {retries}, тишин: {silences}, старых кадров: {stale}")
        if stale:
            print("  .. старых кадров больше нуля: либо на второй машине больше "
                  "одного rx (шторм подтверждений, AGENTS.md 3), либо слот "
                  "забит наследием прежнего обмена")
        for line in list(tail)[-4:]:
            print(f"  | {line}")
        return 0.0

    median = statistics.median(steps)
    total = time.perf_counter() - started_at
    print(f"  подтверждено: {len(steps)}/{frames}, повторов: {retries}, "
          f"тишины: {silences}, NOTE: {notes}, старых кадров: {stale}")
    print(f"  шаг CMD->ACK: медиана {median:6.2f} с, мин {min(steps):6.2f}, "
          f"p95 {pct(steps, 0.95):6.2f} с")
    print("  " + rate(sum(steps), len(steps), payload_size))
    # Время на команду длиннее шага: после ACK tx ещё выжидает reply_wait,
    # собирая пачку NOTE. Для боевой скорости важно именно оно.
    per_command = total / len(steps)
    print(f"  время на команду (с reply-wait): {per_command:6.2f} с -> "
          f"{payload_size / per_command:6.0f} Б/с, 1 МиБ за "
          f"{1024 * 1024 / (payload_size / per_command) / 60:.0f} мин")
    if retries or silences or stale:
        print(f"  !! прогон нечист: {retries} ретрансляций, {silences} тишин, "
              f"{stale} старых кадров — всё это добавляет по --wait и не "
              "измеряет канал. Поднимите --remote-wait, убедитесь, что слот "
              "чист и на второй машине ровно один rx, и повторите")
    return median


# --- слой 6: размер записи против задержки -----------------------------------

# Размеры полезной нагрузки для слоя bulk. 240 — штатный MAX_PAYLOAD, дальше
# столько, сколько понадобилось бы агенту: 4 КБ — кадр под вывод команды,
# 64 КБ — чтобы донести ~16 КБ текста после zlib. Крупнее SNAPSHOT_MAX
# уходит несколькими записями, следующая после ответа на текущую.
BULK_SIZES = (240, 1024, 4096, 16384, 65536)
# Цель, ради которой всё меряется: агент читает 1000 токенов/с, токен ~4 байта.
BULK_TARGET_BPS = 4000.0


def bulk_listen(slot: str, seconds: float, peer_a: str, peer_b: str) -> None:
    """Сторона B: отвечать на большие записи, не разбирая их.

    Разбор кадра здесь сознательно не вызывается. Меряется транспорт — как
    быстро редирект переносит запись разного размера, — а кадростроение для
    этого только помеха: MAX_PAYLOAD = 240 не дал бы записать больше 240 байт,
    то есть измерить ровно то, ради чего слой написан, было бы невозможно.

    Ответ — NOTE с первыми восемью байтами принятой записи. Маркер обязателен:
    на бою ответ прошлого прогона приезжает с опозданием в цикл редиректа и,
    будучи одинаковым по содержанию, был бы принят за ответ текущего — RTT
    вышел бы на пару циклов короче действительного. По маркеру отправитель
    отличает свой ответ от опоздавшего, и дренаж очереди перестаёт быть
    единственным средством защиты.
    """
    print(f"=== bulk listen: жду записи на слоте {slot}, {seconds:g} с ===")
    print(f"  метки: я {peer_b}, собеседник {peer_a}. rx на этой машине на время")
    print("  прогона нужно остановить: два конца с одним peer-id дадут рассинхрон")
    store = open_store(slot)
    rx = ClipboardTransport(store, peer_id=peer_b)
    deadline = time.monotonic() + seconds
    heard = 0
    try:
        while time.monotonic() < deadline:
            raw = rx.receive(1.0)
            if raw is None:
                continue
            heard += 1
            rx.send(framing.build_frame(config.NOTE, 0, bytes(raw[:8])))
            print(f"  #{heard} принято {len(raw)} Б, ответ отправлен")
    except KeyboardInterrupt:
        print("")
    finally:
        rx.close()
    print(f"  всего принято записей: {heard}")


def bulk_payload(size: int, index: int) -> bytes:
    """Содержимое записи для bulk: псевдослучайные байты, seed от времени.

    Два требования, и оба из-за того, что иначе цифра льстит каналу.

    Несжимаемость. RDP-редирект сжимает пересылаемый буфер. Повторяющийся
    заполнитель («xxxx...») сжался бы в десятки раз, и замер показал бы
    пропускную способность сжатия, а не канала — ровно там, где мы решаем,
    спасёт ли батчинг. Байты берутся псевдослучайными, это худший случай и он
    же честный: реальный вывод команд сжимается лучше, и цифру надо читать как
    нижнюю границу.
    """
    return random.Random(int(time.time()) + index).randbytes(size)


def bulk_marker(raw: bytes) -> bytes:
    """Первые восемь байт payload ответа: «на какую запись это ответ».

    Разбор кадра здесь нужен, а не помеха: ответ всегда маленький и валидный,
    в отличие от крупной записи, которую как раз и нельзя собрать в кадр.
    Нераспознанное отдаётся пустым — такое сравнение с маркером не совпадёт, и
    прогон подождёт настоящего ответа вместо того, чтобы засчитать мусор.
    """
    try:
        return framing.parse_frame(raw).payload[:8]
    except framing.FrameError:
        return b""


def bulk_send(slot: str, sizes, wait: float, peer_a: str, peer_b: str) -> None:
    """Сторона A: гонять один размер за раз и смотреть, как растёт RTT.

    Вопрос, ради которого слой существует. «10-15 с» в документации — это
    задержка одного события редиректа, а не его пропускная способность, и
    различить их по штатному обмену нельзя: кадр всегда один и тот же, 240
    байт. Если задержка не зависит от размера, то весь дефицит агента
    закрывается одним приёмом — нести больше байт на один шаг; если растёт
    вместе с размером, батчинг бесполезен и транспорт надо менять.

    История своих ячеек обнуляется перед каждой записью. С нею merge переносит
    в слот предыдущие крупные блоки, текст слота вырастает втрое ровно там, где
    мы как раз и смотрим на размер, — замер стал бы мерить историю, а не запись.

    Содержимое записи меняется от размера к размеру: приёмник считает кадр
    новым по паре (номер, содержимое), и одинаковый заполнитель на разных кругах
    был бы принят за повтор.
    """
    print(f"=== bulk: нагрузка разного размера, слот {slot} ===")
    print(f"  на второй машине (B) должен работать:")
    print(f"    python tools/bench_speed.py --bulk-listen --bulk-wait 600")
    print("  и штатный rx там должен быть остановлен")
    store = open_store(slot)
    tx = ClipboardTransport(store, peer_id=peer_a)
    rows = []
    try:
        for index, size in enumerate(sizes):
            clear_slot(slot)
            time.sleep(0.5)
            # Очередь обязанной стороны вычищается перед каждой записью. Без
            # этого остаток прошлого прогона (ответ, приехавший позже, чем мы
            # засчитали шаг) вылетает из _pending мгновенно, RTT выходит ~0, а
            # скорость — мегабайты в секунду. Проверено: второй прогон подряд
            # давал 2.1 МБ/с на 4096 байтах, и выглядело это как фантастический
            # результат, а не как мусор в очереди.
            while tx.receive(0.0) is not None:
                pass
            payload = bulk_payload(size, index)
            pieces = snapshot_pieces(payload)
            started = time.perf_counter()
            slot_chars = 0
            rtt = None
            missed = False
            for piece in pieces:
                # Прошлый ответ уже засчитан. Остаток очереди не должен
                # стать «мгновенным» RTT следующего куска.
                while tx.receive(0.0) is not None:
                    pass
                tx._my_history.clear()
                marker = piece[:8]
                tx._publish(piece)
                slot_chars = len(store.read() or "")
                # Дедлайн на всю нагрузку, не на кусок: два ожидаемых цикла
                # сверху, первый мог уйти на опоздавший ответ прошлого прогона.
                deadline = started + wait * 2
                while time.perf_counter() < deadline:
                    got = tx.receive(1.0)
                    if got is None:
                        continue
                    if bulk_marker(got) == marker:
                        rtt = time.perf_counter() - started
                        break
                else:
                    missed = True
                    break
            if missed or rtt is None:
                tx._nudge_at = None
                tx._nudge_mine = None
                print(f"  {size:>6} Б: ответа нет за {wait * 2:g} с — прогон "
                      "прерван (rx не отвечает или ждёт дольше)")
                break
            bps = size / rtt
            rows.append((size, slot_chars, rtt, bps, len(pieces)))
            parts = f"  кусков {len(pieces)}" if len(pieces) > 1 else ""
            print(f"  {size:>6} Б  слот {slot_chars:>7} симв  "
                  f"RTT {rtt:6.2f} с  {bps:7.0f} Б/с"
                  f"  {'ЦЕЛЬ' if bps >= BULK_TARGET_BPS else ''}"
                  f"{parts}")
    finally:
        tx.close()

    if len(rows) < 2:
        print("  вывод: сравнение не состоялось, нужно минимум два размера")
        return

    base = rows[0]
    best = max(rows, key=lambda row: row[3])
    print()
    print(f"  RTT: {base[2]:.2f} с на {base[0]} Б -> {rows[-1][2]:.2f} с на "
          f"{rows[-1][0]} Б, то есть объём вырос в "
          f"{rows[-1][0] / base[0]:.0f}x, а время в {rows[-1][2] / base[2]:.1f}x")
    print(f"  лучший пропускной поток: {best[3]:.0f} Б/с на записи {best[0]} Б "
          f"(агенту нужно {BULK_TARGET_BPS:.0f})")
    if rows[-1][4] > 1:
        print(f"  крупнее {SNAPSHOT_MAX} Б одной записью редирект не увозит: "
              f"{rows[-1][0]} Б ушли {rows[-1][4]} записями, "
              "следующая после ответа на текущую.")
    elif rows[-1][2] / base[2] < rows[-1][0] / base[0] / 2:
        print("  задержка почти не зависит от размера: это латентность события,")
        print("  а не пропускная способность. Спасает батчинг — нести больше")
        print("  байт на один шаг (MAX_PAYLOAD, один ACK на пачку, zlib).")
    else:
        print("  задержка растёт вместе с размером: это пропускная способность,")
        print("  и батчинг её не обманет. Транспорт под агента надо менять")
        print("  (или ограничивать вывод команд до десятков байт на шаг).")


# --- слои 3 и 4: два процесса -------------------------------------------------


def start_cli(module: str, extra: list[str]):
    return subprocess.Popen(
        [PY, "-m", module] + extra, cwd=ROOT, env=ENV, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
    )


def baseline_seconds(channel: list[str], tries: int = 3) -> float:
    """Сколько стоит запустить tx с пустым вводом: это вычитается из прогона.

    Иначе полсекунды старта интерпретатора и поднятия слота приписались бы
    скорости канала, и на коротком прогоне цифра была бы неправдой.
    """
    samples = []
    for _ in range(tries):
        started = time.perf_counter()
        subprocess.run([PY, "-m", "rdpconn.tx"] + channel + ["--repl"],
                       cwd=ROOT, env=ENV, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=60)
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def bench_processes(slot: str, frames: int, payload_size: int,
                    label: str) -> float:
    """Возвращает измеренный шаг CMD->ACK в миллисекундах (0 — обменов не было)."""
    print(f"=== {label}: два процесса, {frames} команд по {payload_size} Б ===")
    tmp = None
    if slot == "file":
        tmp = tempfile.mkdtemp(prefix="rdpconn_bench_")
        slot_spec = f"file:{os.path.join(tmp, 'slot.txt')}"
    else:
        slot_spec = slot

    channel = ["--slot", slot_spec, "--ping", "0", "--wait", "5"]
    rx = start_cli("rdpconn.rx", channel + ["--peer-id", "B", "--no-echo",
                                            "--duration", "600"])
    time.sleep(1.5)  # приёмник обязан поднять слот раньше, чем придёт первый CMD

    started = time.perf_counter()
    tx = start_cli("rdpconn.tx", channel + ["--peer-id", "A", "--repl",
                                            "--reply-wait", "0.02"])
    try:
        tx.stdin.writelines(f"@{i} " + "x" * (payload_size - 4) + "\n"
                            for i in range(frames))
        tx.stdin.close()
        out, err = tx.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        tx.kill()
        out, err = tx.communicate()
    spent = time.perf_counter() - started
    rx.terminate()
    rx_out, _ = rx.communicate(timeout=30)

    baseline = baseline_seconds(channel)
    acks = out.count("<< ACK")
    retries = out.count("попытка 2") + out.count("попытка 3")
    net = max(0.001, spent - baseline)
    # Считаем шаг по числу ОТПРАВЛЕННЫХ команд, а не по числу ACK: на повтор
    # CMD приёмник отвечает закешированным ACK (docs/protocol.md 4), поэтому
    # подтверждения считаются чаще, чем было шагов, и деление на них занижало бы
    # задержку ровно на величину ретрансляций. Недошедшие команды время тоже
    # заняли, поэтому в знаменателе — именно frames.
    print(f"  подтверждений: {acks} (из {frames} команд), повторов: {retries}, "
          f"старт процесса вычтен: {baseline:.2f} с")
    if acks == 0:
        print("  FAIL: обменов нет. tx: " + " | ".join(out.splitlines()[-3:]))
        print("  rx: " + " | ".join(rx_out.splitlines()[-3:]))
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        return 0.0
    step_ms = net / frames * 1000.0
    print(f"  время прогона: {net:.2f} с на {frames} шагов CMD->ACK")
    print(f"  RTT шага: {step_ms:6.0f} мс (средний)")
    print("  " + rate(net, frames, payload_size))
    good = frames * payload_size / net
    print(f"  1 МиБ полезного текста: {1024 * 1024 / good / 60:.1f} мин")
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return step_ms


# --- пересчёт на боевой стенд -------------------------------------------------


def extrapolate(step_ms: float) -> None:
    """Что даст измеренный шаг, когда доминировать станет редирект буфера.

    Задержка редиректа добавляется к каждому шагу CMD->ACK, поэтому скорость
    считается как payload / (измеренный шаг + задержка), а не как-то тоньше:
    на одном шаге уходит ровно один кадр полезного текста.
    """
    print("=== пересчёт на RDP-редирект (сам по себе 10-15 с, docs/protocol.md 5) ===")
    print("  сверх шага      шаг CMD->ACK   payload Б/с   1 МиБ полезного")
    for extra in (0.0, 0.5, 2.0, 10.0, 15.0):
        step = step_ms / 1000.0 + extra
        bps = config.MAX_PAYLOAD / step
        print(f"  +{extra:7.1f} с    {step:10.2f} с   {bps:10.1f}   "
              f"{1024 * 1024 / bps / 60:9.1f} мин")
    print("  вывод: выше ~0.1 с редиректа скоростью правит он, а не код;")
    print("  поднимать её можно только больше payload на шаг (MAX_PAYLOAD,")
    print("  несколько CMD на один ACK), а не оптимизацией разбора")


def main() -> int:
    p = argparse.ArgumentParser(description="Замер скорости канала rdp_conn")
    p.add_argument("--frames", type=int, default=200,
                   help="кадров в слоях cpu/poll (по умолчанию 200)")
    p.add_argument("--proc-frames", type=int, default=40,
                   help="команд в прогоне: двумя процессами и в бою "
                        "(по умолчанию 40)")
    p.add_argument("--payload", type=int, default=config.MAX_PAYLOAD,
                   help=f"размер payload (по умолчанию {config.MAX_PAYLOAD})")
    p.add_argument("--win", action="store_true",
                   help="добавить прогон через настоящий буфер обмена этой машины")
    p.add_argument("--cpu-only", action="store_true",
                   help="только слои cpu и poll, без процессов")
    p.add_argument("--rtt-only", action="store_true",
                   help="только задержка шага, без процессов (см. слой rtt)")
    p.add_argument("--rtt-rounds", type=int, default=40,
                   help="раундов в слое rtt (по умолчанию 40)")
    p.add_argument("--rtt-slot", default="win",
                   help="слот для слоя rtt: win | memory | file:PATH "
                        "(по умолчанию win)")
    p.add_argument("--poll", type=float, default=None,
                   help="подменить POLL_S на время замера, чтобы увидеть цену "
                        "опроса (по умолчанию — как в коде)")
    p.add_argument("--remote", action="store_true",
                   help="бой: rx на второй машине, отсюда только tx "
                        "(единственный режим, который меряет редирект RDP)")
    p.add_argument("--remote-wait", type=float, default=REMOTE_WAIT_S,
                   help=f"сколько секунд ждать подтверждения в бою "
                        f"(по умолчанию {REMOTE_WAIT_S:g}; меньше "
                        f"{REMOTE_MIN_WAIT_S:g} — замер измерит ретрансляции)")
    p.add_argument("--remote-reply-wait", type=float, default=None,
                   help="сколько секунд собирать NOTE после ACK в бою. По "
                        "умолчанию 0 без --echo и 3 с ним: rx с --no-echo "
                        "молчит, и ожидание обошлось бы дороже самого шага")
    p.add_argument("--peer-a", default="A", help="метка этой стороны")
    p.add_argument("--peer-b", default="B", help="метка второй стороны")
    p.add_argument("--echo", action="store_true",
                   help="rx на второй машине работает с эхом (без --no-echo): "
                        "тогда шаг включает NOTE-эхо и reply-wait по умолчанию 3")
    p.add_argument("--bulk", action="store_true",
                   help="бой: RTT одной записи разного размера — отвечает ли "
                        "редирект за латентность или за пропускную способность")
    p.add_argument("--bulk-listen", action="store_true",
                   help="сторона B для --bulk: отвечать на крупные записи")
    p.add_argument("--bulk-wait", type=float, default=120.0,
                   help="сколько секунд ждать ответ в --bulk и сколько слушать "
                        "в --bulk-listen (по умолчанию 120)")
    p.add_argument("--bulk-sizes", default=None,
                   help="размеры записей в байтах через запятую "
                        f"(по умолчанию {','.join(map(str, BULK_SIZES))})")
    args = p.parse_args()

    payload_size = min(args.payload, config.MAX_PAYLOAD)
    if payload_size != args.payload:
        print(f".. payload ограничен MAX_PAYLOAD={config.MAX_PAYLOAD}")

    print(f"python {sys.version.split()[0]}, кадр {5 + payload_size + 2} Б, "
          f"MAX_PAYLOAD={config.MAX_PAYLOAD}, POLL_S={POLL_S}")
    print()

    if args.bulk_listen:
        bulk_listen("win", args.bulk_wait, args.peer_a, args.peer_b)
        return 0

    if args.bulk:
        sizes = ([int(x) for x in args.bulk_sizes.split(",")]
                 if args.bulk_sizes else list(BULK_SIZES))
        bulk_send("win", sizes, args.bulk_wait, args.peer_a, args.peer_b)
        return 0

    if args.remote:
        # Редирект живёт между машинами: никакой локальный слой его не
        # заменяет, поэтому для стенда годится только этот режим.
        bench_remote("win", args.proc_frames, payload_size, args.remote_wait,
                     args.remote_reply_wait if args.remote_reply_wait is not None
                     else (3.0 if args.echo else 0.0),
                     args.peer_a, args.peer_b, args.echo)
        return 0

    if args.rtt_only:
        # Режим для стенда: один процесс, никаких дочерних, поэтому его можно
        # запустить и внутри RDP-сессии, где лишние процессы только мешают.
        if args.poll is not None:
            clipboard_mod.POLL_S = args.poll
        bench_rtt(args.rtt_slot, args.rtt_rounds, payload_size)
        return 0

    bench_cpu(args.frames, payload_size)
    print()
    bench_poll(args.frames, payload_size)
    print()
    # Цена опроса на том же memory-слоте: слой намеренно повторяет poll, но с
    # получателем в отдельном потоке, и разница двух слоёв и есть стоимость
    # POLL_S. Буфер в полном прогоне не трогаем — у него свой слой ниже и свой
    # rtt-режим, чтобы фоновые копии человека не лезли в каждый замер.
    # --poll доходит только сюда: процессы proc живут своим импортом и своей
    # константой, подменять им опрос незачем — их шаг меряется как есть.
    bench_rtt("memory", args.rtt_rounds, payload_size, poll=args.poll)
    print()
    # Пересчёт считаем от шага двух процессов: он включает и опрос, и переключение
    # между процессами. Шаг одного процесса для этого оптимистичен.
    measured_ms = 0.0
    if not args.cpu_only:
        measured_ms = bench_processes("file", args.proc_frames, payload_size,
                                      "proc (file:PATH)")
        print()
        if args.win:
            try:
                open_store("win")
            except Exception as exc:  # не-Windows: слой просто недоступен
                print(f"=== win: пропущен, буфер обмена недоступен ({exc}) ===")
            else:
                win_ms = bench_processes("win", args.proc_frames, payload_size,
                                         "win (CF_UNICODETEXT)")
                measured_ms = win_ms or measured_ms
            print()
    if measured_ms:
        extrapolate(measured_ms)
    return 0


if __name__ == "__main__":
    sys.exit(main())

