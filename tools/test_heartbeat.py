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

from rdpconn import app, clipboard as cb, rx
from rdpconn import config, framing

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

print("RESULT:", "FAIL\n  - " + "\n  - ".join(fails) if fails else "ALL GREEN")
sys.exit(1 if fails else 0)
