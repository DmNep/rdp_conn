"""Proverka heartbeat: PING/PONG, detekt molchaniya, vosstanovlenie.

Chto dokazyvaet:

  - PING sobosednika poluchaet PONG s tem zhe seq (inache zhivoy kanal
    ob'yavlyayetsya mertvym);
  - lyuboy prinyatyy kadr — priznak zhizni, i pinging ne tratitsya na zhivogo
    sobosednika, kotoryy chestno otvechaet NOTE;
  - molchanie dol'she poroga ob'yavlyaetsya obryvom odin raz, a ne kazhdyy
    prohod cikla;
  - posle obryva transport zabyl prinyatye nomera (resync), inache
    perezapustivshiysya sobosednik molchal by vechno;
  - --ping 0 polnost'yu vyklyuchaet heartbeat.

Test — skript, ne pytest: zavisimostey net, dostano python 3.9+.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rdpconn import app, clipboard as cb, rx, tx
from rdpconn import config, framing, segment

fails = []


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


class Sink:
    """Transport-zaglushka: sobiraet otpravlennoe i otdает zadannoe."""

    def __init__(self, frames=()):
        self.sent = []
        self.inbox = list(frames)
        self.resets = 0

    def send(self, data):
        self.sent.append(data)

    def receive(self, timeout):
        if self.inbox:
            return self.inbox.pop(0)
        return None

    def reset_seen(self):
        self.resets += 1


print("=== 1. PING -> PONG s tem zhe seq ===")
hb = app.Heartbeat(interval=10.0)
sink = Sink([framing.ping(7)])
handled = app.heartbeat_handle(hb, sink, framing.parse_frame(sink.inbox[0]) if sink.inbox else None)
# Kаdr uzhe vygnan iz inbox chteniem vyshe, poetomu proverim cherez novyy.
sink2 = Sink()
ping_frame = framing.parse_frame(framing.ping(7))
check("PING pogloshchen", app.heartbeat_handle(hb, sink2, ping_frame))
check("PONG otpravlen", len(sink2.sent) == 1 and sink2.sent[0] == framing.pong(7),
      f"sent={[d.hex() for d in sink2.sent]}")

print("=== 2. PONG ot sobosednika pogloshchaetsya i schitaetsya zhizn'yu ===")
hb = app.Heartbeat(interval=10.0)
sink = Sink()
check("PONG pogloshchen", app.heartbeat_handle(hb, sink, framing.parse_frame(framing.pong(3))))
check("nichego ne otvetili", not sink.sent, "PONG na PONG — beskonechnyy obmen")

print("=== 3. chuzhoy kadr heartbeat ne trogaet ===")
hb = app.Heartbeat(interval=10.0)
sink = Sink()
note = framing.parse_frame(framing.build_frame(config.NOTE, 1, b"hi"))
check("NOTE propushchen naruzhu", not app.heartbeat_handle(hb, sink, note))

print("=== 4. vyklyuchennyy heartbeat nichego ne delaet ===")
hb = app.Heartbeat(interval=0.0)
sink = Sink()
check("enabled=False", not hb.enabled)
check("PING ne pogloshchaetsya", not app.heartbeat_handle(hb, sink, ping_frame),
      "inache pri --ping 0 PING nikto ne obrabotaet")
check("tick molchit", not app.heartbeat_tick(hb, sink))
check("obryv ne ob'yavlyaetsya", not app.heartbeat_check(hb, sink))

print("=== 5. tick shlet PING po intervalu, a ne kazhdyy raz ===")
hb = app.Heartbeat(interval=0.2)
sink = Sink()
check("srazu posle starta ne pora", not app.heartbeat_tick(hb, sink), "interval ne proshel")
time.sleep(0.25)
check("posle intervala pora", app.heartbeat_tick(hb, sink))
check("PING ushel", len(sink.sent) == 1 and sink.sent[0] == framing.ping(0))
check("srazu vtorym razom ne poshel", not app.heartbeat_tick(hb, sink))

print("=== 6. detekt molchaniya: odin raz, a ne kazhdyy prohod ===")
hb = app.Heartbeat(interval=0.1, misses=2)
sink = Sink()
check("zhiv po umolchaniyu", not hb.expired())
time.sleep(0.25)
check("molchanie prezvysheno", hb.expired())
check("obryv ob'yavlen", app.heartbeat_check(hb, sink))
check("transport sbrosil pometki", sink.resets == 1, f"resets={sink.resets}")
check("povtorno ne ob'yavlyaetsya", not app.heartbeat_check(hb, sink),
      "inache soobshchenie povtoryalos' by kazhdye 50 ms")

print("=== 7. zhivoy sobosednik sbrasyvaet schetchik molchaniya ===")
hb = app.Heartbeat(interval=0.1, misses=3)
sink = Sink()
time.sleep(0.15)
check("note() otmechaet zhizn'", hb.note() is False, "pervyy raz ne vozrozhdenie")
check("molchanie sbrosheno", not hb.expired())
check("zhiv", hb.alive)

print("=== 8. vozvrashchenie posle obryva ===")
hb = app.Heartbeat(interval=0.1, misses=2)
sink = Sink()
time.sleep(0.25)
app.heartbeat_check(hb, sink)
check("obryv", not hb.alive)
check("vozrozhdenie zamecheno", hb.note() is True,
      "soobshchenie 'snova otvechaet' pechataetsya tol'ko pri vozrozhdenii")

print("=== 9. heartbeat ne tratit limit sobytiy (integraciya s rx) ===")
cb.MemoryClipboard.reset()
a = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
b = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
hb_b = app.Heartbeat(interval=0.2)
a.send(framing.ping(5))
frame, err = app.receive_frame(b, 0.3)
check("rx poluchil PING", frame is not None and frame.type == config.PING)
check("PING pogloshchen heartbeat-om", app.heartbeat_handle(hb_b, b, frame))
back, _ = app.receive_frame(a, 0.3)
check("A poluchil PONG", back is not None and back.type == config.PONG and back.seq == 5,
      f"{back}")
a.close(); b.close()

print("=== 10. posle obryva prinyatie vozobnovlyaetsya (resync) ===")
cb.MemoryClipboard.reset()
a = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
b = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
a.send(framing.build_frame(config.CMD, 0, b"one"))
check("pervyy kadr prinyat", b.receive(timeout=0.3) is not None)
hb_b = app.Heartbeat(interval=0.1, misses=2)
time.sleep(0.25)
app.heartbeat_check(hb_b, b)
# Sobsednik perezapustilsya: tot zhe seq, drugoy tekst.
a2 = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
a2.send(framing.build_frame(config.CMD, 0, b"two"))
got = b.receive(timeout=0.3)
check("posle resync novyy kadr prinyat", got == framing.build_frame(config.CMD, 0, b"two"),
      f"got={got!r}")
a2.close(); a.close(); b.close()

print("=== 11. heartbeat ne gulyaet prinYatyy kadr (regress pervoy popytki CMD) ===")
# Boevoy sluchay: A otnosil CMD, B ego prochital — i v tot zhe prohod cikla
# uspival PING. Sleduyushchii za tickom continue uvol cikl v nachalo, mimo
# handle(), i komanda propala bez sleda; spasal tol'ko povtor protokola.
#
# Pining zdes' podmenen, a ne zhdan po vremeni: vremya v teste delalo by
# proverku plavayushchei (ona to prohodila, to padala). Podmena zhurnal'no i
# est' tot boevoy sluchay — tick otpravil kadr v tom zhe prohode, chto i CMD.
args = rx.build_parser().parse_args(["--slot", "memory"])
hb = app.Heartbeat(interval=0.01)
sink = Sink()
tick_calls = []
real_tick = app.heartbeat_tick


def tick_now(hb, transport):
    tick_calls.append(1)
    transport.send(framing.ping(9))
    return True


app.heartbeat_tick = tick_now
try:
    cmd = framing.parse_frame(framing.build_frame(config.CMD, 0, b"hi"))
    counted = rx.step(args, sink, hb, cmd, None)
finally:
    app.heartbeat_tick = real_tick
sent = [framing.parse_frame(d).type_name for d in sink.sent]
check("pining ushel v etom zhe prohode", tick_calls == [1], f"tick={tick_calls}")
check("CMD vse ravno dooshel do handle", "ACK" in sent,
      f"sent={sent} — inache kadr vykinsya v tochnosti kak v boevom protone")
check("eho tozhe otpravleno", "NOTE" in sent, f"sent={sent}")
check("sobytie zachitano", counted, "inache --limit s'edalsya by nechem")

print("--- 11d. povtor togo zhe CMD ne ispolnyaetsya snova ---")
exec_args = rx.build_parser().parse_args(["--slot", "memory", "--exec"])
exec_hb = app.Heartbeat(interval=0)
exec_sink = Sink()
ran = []
real_run = rx._run_command


class _Done:
    returncode = 0
    stdout = b"once\r\n"
    stderr = b""


def fake_run(text, timeout):
    ran.append(text)
    return _Done()


rx._run_command = fake_run
try:
    cache = rx.ReplyCache()
    same = framing.parse_frame(framing.cmd(0, "echo once"))
    rx.step(exec_args, exec_sink, exec_hb, same, None, cache=cache)
    rx.step(exec_args, exec_sink, exec_hb, same, None, cache=cache)
    other = framing.parse_frame(framing.cmd(0, "echo other"))
    rx.step(exec_args, exec_sink, exec_hb, other, None, cache=cache)
finally:
    rx._run_command = real_run
check("odin seq s tem zhe tekstom — odin zapusk",
      ran == ["echo once", "echo other"], str(ran))
ack_n = sum(1 for raw in exec_sink.sent
            if framing.parse_frame(raw).type == config.ACK)
check("ACK ushel na kazhdyi priyom, vklyuchaya povtor", ack_n == 3, f"ack={ack_n}")

print("--- 11e. ACK posle obryva vozvrashchaet tx k zhizni ---")
back_args = tx.build_parser().parse_args(
    ["--slot", "memory", "--ping", "10", "--wait", "1",
     "--reply-wait", "0.05", "--retries", "1", "hi"])
back_hb = app.Heartbeat(interval=10)
back_hb.mark_lost()
back_sink = Sink([framing.ack(0)])
ok = tx.send_one(back_args, back_sink, app.SeqCounter(), "hi", back_hb)
check("obmen podtverzhden", ok)
check("poterya snyata kadrom ACK", back_hb.alive)

print("--- 11b. sluzhebnye kadry step pogloshchaet, ne schitaya sobytiem ---")
hb = app.Heartbeat(interval=10.0)
sink = Sink()
check("PING ne sobytie", not rx.step(args, sink, hb,
                                     framing.parse_frame(framing.ping(4)), None))
check("PONG otpravlen", sink.sent == [framing.pong(4)], f"sent={sink.sent}")
check("PONG ne sobytie", not rx.step(args, sink, hb,
                                     framing.parse_frame(framing.pong(4)), None))
check("otvet na PONG net", len(sink.sent) == 1, "inache by bylo beskonechnoe handshake")
check("tishina ne sobytie", not rx.step(args, sink, hb, None, None))

print("--- 11c. priyom kadra otodvigaet sobstvennyi pining ---")
# Esli eto ne tak, tick vsegda uspevaet mezhdu priyomom i obrabotkoi, i
# lyuboi continue v etom prohode stavit pod udar kazhdyi kadr.
hb = app.Heartbeat(interval=0.1)
time.sleep(0.15)
check("do prikhoda kadra pining sovsem pora", hb.due())
hb.note()
check("posle note() pining ne pora", not hb.due(),
      "zhivoy obmen uzhe dokazatel'stvo zhivosti")

print()
print("=== 12. otvetnaya storona svoih PING ne zavodit ===")
# Dvustoronnii pingu udvaivaet zapisi v slot, a kazhdaia zapis v bufernom
# kanale — eto eshche i sobytie RDP-redirekta. Na lagayushchem redirekte eto
# prevrashchaet heartbeat v fonovii shum, kotoryi sam zhe meshayet obmenu.
hb = app.Heartbeat(interval=0.01, proactive=False)
sink = Sink()
time.sleep(0.05)
check("due() lozhnaya", not hb.due())
check("tick nichego ne otpravil", not app.heartbeat_tick(hb, sink),
      f"sent={[d.hex() for d in sink.sent]}")
check("na chuzhoi PING otvetil", app.heartbeat_handle(hb, sink, ping_frame),
      "otvechat' on vse ravno dolzhen")
check("PONG otpravlen", sink.sent == [framing.pong(7)], f"sent={sink.sent}")
# Porog molchaniya pri etom rabotaet: otvetnaya storona obryv zametit.
check("porog molchaniya ostalsya", hb.deadline > 0)

print("=== 12b. malyi porog oznachaetsya, a ne meraetsya vsleduyu ===")
# Bez preduprezhdeniya chelovek poluchaet 'sobesednik molchit 15.2 s' na
# zhivom kanale i perestayet veryt preduprezhdeniyam voobshche.
import argparse
ns = argparse.Namespace(ping=5.0)
hb = app.make_heartbeat(ns)
check("porog = interval * misses", hb.deadline == 15.0, f"{hb.deadline}")
check("poredel'nyy porog opoznan", hb.deadline < app.MIN_SILENCE_S,
      "inache lozhnyy obryv nikak ne otmechen")
hb2 = app.make_heartbeat(argparse.Namespace(ping=25.0))
check("boevoy porog normalen", hb2.deadline >= app.MIN_SILENCE_S, f"{hb2.deadline}")


def _tx_args(wait="0.4", retries="1", reply="0.05"):
    return tx.build_parser().parse_args(
        ["--slot", "memory", "--ping", "0", "--wait", wait,
         "--reply-wait", reply, "--retries", retries, "hi"])


print("=== 13. NOTE s tem zhe seq zakryvaet popytku ===")
# Na stende otvet priezzhal, a ACK v snimke bufera ne bylo. Zhdat ACK i
# slat komandu snova — eto vtoraya dostavka uzhe poluchennogo otveta.
note_sink = Sink([framing.build_frame(config.NOTE, 0, "S5-FIRST".encode("cp866"))])
note_ok = tx.send_one(_tx_args(), note_sink, app.SeqCounter(), "echo S5-FIRST",
                      app.Heartbeat(0))
check("NOTE togo zhe seq zakryvaet obmen", note_ok)
check("komanda ushla odnim kadrom", len(note_sink.sent) == 1, f"sent={len(note_sink.sent)}")

print("=== 14. chuzhoy NOTE i chuzhoy ACK popytku ne zakryvayut i ne szhigayut ===")
foreign = Sink([
    framing.build_frame(config.NOTE, 9, b"old"),
    framing.ack(4),
    framing.ack(0),
])
foreign_ok = tx.send_one(_tx_args(wait="1"), foreign, app.SeqCounter(), "hi",
                         app.Heartbeat(0))
check("posle chuzhogo kadra svoy ACK eshche prinimaetsya", foreign_ok)
only_foreign = Sink([framing.build_frame(config.NOTE, 9, b"old")])
only_ok = tx.send_one(_tx_args(wait="0.3"), only_foreign, app.SeqCounter(), "hi",
                      app.Heartbeat(0))
check("odin chuzhoy NOTE ne uspekh", not only_ok)

print("=== 15. komanda dlinnee kadra ne podnimaet ValueError ===")


class ReplySink(Sink):
    """Na kazhdyi CMD kladet ACK, a na poslednii kusok eshche i NOTE."""

    def send(self, data):
        self.sent.append(data)
        frame = framing.parse_frame(data)
        if frame.type != config.CMD:
            return
        self.inbox.append(framing.ack(frame.seq))
        parsed = segment.unpack(frame.payload)
        if parsed is None or parsed[1] + 1 == parsed[2]:
            self.inbox.append(framing.build_frame(config.NOTE, frame.seq, b"DONE"))


class CountSink(ReplySink):
    def __init__(self):
        super().__init__()
        self.writes = 0

    def send_many(self, frames):
        self.writes += 1
        for raw in frames:
            self.send(raw)


long_text = "echo " + ("Z" * 300)
long_sink = CountSink()
long_ok = tx.send_one(_tx_args(wait="1", reply="0.05"), long_sink, app.SeqCounter(),
                      long_text, app.Heartbeat(0))
check("oba kuska — odna zapis", long_sink.writes == 1, f"writes={long_sink.writes}")
long_cmds = [framing.parse_frame(raw) for raw in long_sink.sent
             if framing.parse_frame(raw).type == config.CMD]
check("dlinnaya podtverzhdena", long_ok)
check("kuskov bolshe odnogo", len(long_cmds) >= 2, f"cmds={len(long_cmds)}")
check("kazhdyi kusok vlezaet v kadr",
      all(len(frame.payload) <= config.MAX_PAYLOAD for frame in long_cmds))
try:
    framing.build_frame(config.CMD, 0, long_text.encode("cp866"))
    check("syroy tekst dlinnee kadra po-prezhnemu ValueError", False)
except ValueError:
    check("syroy tekst dlinnee kadra po-prezhnemu ValueError", True)

short_sink = ReplySink()
short_ok = tx.send_one(_tx_args(), short_sink, app.SeqCounter(), "echo hi",
                       app.Heartbeat(0))
short_cmds = [framing.parse_frame(raw) for raw in short_sink.sent
              if framing.parse_frame(raw).type == config.CMD]
check("korotkaya — odin CMD", short_ok and len(short_cmds) == 1, f"n={len(short_cmds)}")
check("korotkaya bez zagolovka segmenta",
      bool(short_cmds) and not short_cmds[0].payload.startswith(segment.MAGIC))

print("=== 16. priemnik sobiraet kuski i zapuskaet komandu odin raz ===")
assembly = segment.Assembly()
asm_cache = rx.ReplyCache()
asm_args = rx.build_parser().parse_args(["--slot", "memory", "--exec"])
asm_hb = app.Heartbeat(interval=0)
asm_sink = Sink()
ran_long = []


def fake_long(text, timeout):
    ran_long.append(text)

    class _Out:
        returncode = 0
        stdout = b"once\r\n"
        stderr = b""

    return _Out()


rx._run_command = fake_long
try:
    raw = long_text.encode("cp866")
    chunks = segment.split(raw)
    seqs = list(range(len(chunks)))
    payloads = [segment.pack(seqs[0], i, len(chunks), chunk)
                for i, chunk in enumerate(chunks)]
    for seq, payload in zip(seqs, payloads):
        frame = framing.parse_frame(framing.build_frame(config.CMD, seq, payload))
        rx.step(asm_args, asm_sink, asm_hb, frame, None, cache=asm_cache,
                assembly=assembly)
    last = framing.parse_frame(framing.build_frame(config.CMD, seqs[-1], payloads[-1]))
    rx.step(asm_args, asm_sink, asm_hb, last, None, cache=asm_cache,
            assembly=assembly)
    plain = framing.parse_frame(framing.cmd(9, "echo hi"))
    rx.step(asm_args, asm_sink, asm_hb, plain, None, cache=asm_cache,
            assembly=assembly)
finally:
    rx._run_command = real_run
check("dlinnaya zapuschena odin raz i celikom",
      ran_long == [long_text, "echo hi"], str(ran_long))
ack_rows = [framing.parse_frame(raw) for raw in asm_sink.sent
            if framing.parse_frame(raw).type == config.ACK]
check("ACK na kazhdyi kusok i na povtor poslednego",
      len(ack_rows) >= len(chunks) + 1, f"ack={len(ack_rows)} chunks={len(chunks)}")

print("=== 17. kuski uzhe v ocheredi — odin otvet, bez promezhutochnogo ACK ===")


class HoldSink(Sink):
    def __init__(self):
        super().__init__()
        self.queued = []
        self.writes = 0

    def peek_pending(self):
        return self.queued[0] if self.queued else None

    def pop_pending(self):
        return self.queued.pop(0) if self.queued else None

    def send_many(self, frames):
        self.writes += 1
        for raw in frames:
            self.sent.append(raw)


hold = HoldSink()
hold_cache = rx.ReplyCache()
hold_asm = segment.Assembly()
hold_ran = []


def fake_hold(text, timeout):
    hold_ran.append(text)

    class _Out:
        returncode = 0
        stdout = b"joined\r\n"
        stderr = b""

    return _Out()


rx._run_command = fake_hold
try:
    raw = long_text.encode("cp866")
    chunks = segment.split(raw)
    payloads = [segment.pack(0, i, len(chunks), chunk) for i, chunk in enumerate(chunks)]
    first = framing.parse_frame(framing.build_frame(config.CMD, 0, payloads[0]))
    for seq, payload in enumerate(payloads[1:], start=1):
        hold.queued.append(framing.build_frame(config.CMD, seq, payload))
    rx.step(asm_args, hold, asm_hb, first, None, cache=hold_cache, assembly=hold_asm)
finally:
    rx._run_command = real_run
hold_types = [framing.parse_frame(raw).type for raw in hold.sent]
check("odna zapis otveta", hold.writes == 1, f"writes={hold.writes}")
check("komanda sobrana do zapuska", hold_ran == [long_text], str(hold_ran))
check("v otvete est ACK i NOTE", config.ACK in hold_types and config.NOTE in hold_types,
      str(hold_types))

print("=== 17. dolgiy otvet povtoryaetsya, korotkiy net ===")


class _Nudge:
    def __init__(self):
        self._nudge_once = False


slow = _Nudge()
rx.arm_slow_reply(slow, rx.SLOW_REPLY_S)
check("dolgiy otvet zakazyvaet odin povtor", slow._nudge_once)
fast = _Nudge()
rx.arm_slow_reply(fast, 0.2)
check("korotkiy otvet ne povtoryaetsya", not fast._nudge_once)

print("=== 19. korotkie komandy delyat odin snimok ===")
cb.MemoryClipboard.reset()
batch_a = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
batch_b = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
batch_a.send_many([
    framing.cmd(0, "one"),
    framing.cmd(1, "two"),
])
first, _ = app.receive_frame(batch_b, 0.3)
batch_args = rx.build_parser().parse_args(["--slot", "memory", "--ping", "0"])
rx.handle(batch_args, batch_b, first, None, cache=rx.ReplyCache(),
          assembly=segment.Assembly())
back = []
for _ in range(6):
    got, _ = app.receive_frame(batch_a, 0.1)
    if got is None:
        break
    back.append(got)
ack_seqs = [f.seq for f in back if f.type == config.ACK]
check("oba ACK v odnom otvete", ack_seqs == [0, 1], f"acks={ack_seqs}")
check("echo obeih komand", [f.seq for f in back if f.type == config.NOTE] == [0, 1])
batch_a.close()
batch_b.close()

groups = tx.groups(["aa", "bb", "cc"])
check("tri korotkih — odin snimok", groups == [["aa", "bb", "cc"]], str(groups))
wide = tx.groups(["x" * 300, "yy"])
check("dlinnaya komanda edet otdelno", len(wide) == 2 and wide[1] == ["yy"], str(wide))


class BatchSink(Sink):
    def __init__(self):
        super().__init__()
        self.writes = 0

    def send_many(self, frames):
        self.writes += 1
        self.sent.extend(frames)
        for raw in frames:
            cmd = framing.parse_frame(raw)
            self.inbox.append(framing.ack(cmd.seq))
            self.inbox.append(framing.build_frame(config.NOTE, cmd.seq, cmd.payload))


batch_sink = BatchSink()
batch_tx = tx.build_parser().parse_args(
    ["--slot", "memory", "--ping", "0", "--wait", "1",
     "--reply-wait", "0.05", "--retries", "1", "a", "b"])
sent_ok = tx.send_batch(
    batch_tx, batch_sink, app.SeqCounter(), ["a", "b"], app.Heartbeat(interval=0))
check("dve komandy — odna zapis", sent_ok and batch_sink.writes == 1,
      f"ok={sent_ok} writes={batch_sink.writes}")

print("RESULT:", "FAIL\n  - " + "\n  - ".join(fails) if fails else "ALL GREEN")
sys.exit(1 if fails else 0)
