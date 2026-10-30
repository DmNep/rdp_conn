"""Skvoznoy progon kadra protokola cherez clipboard-kanal (bez vtoroy mashiny).

Chto dokazyvaet: ClipboardTransport soblyudaet tot zhe kontrakt, chto i
AudioTransport: send(bytes) / receive(timeout) -> bytes | None, i kadr
protokola prihodit bayt-v-bayt cherez tekstovyy slot. Otdelno proveryayutsya
tri veshchi, kotorykh u zvuka net vovse:

  - svoy kadr ne prinimaetsya za chuzhoi (seq rastet i na nashu zapis);
  - chuzhaya kopiya ne razbiraetsya i stavit obmen na pauzu;
  - na pauze send zaderzhivaetsya, receive ne daet kadr, a posle vozvrata restore
    kladet poslednii nash kadr obratno v slot.

Test - skript, ne pytest: pytest v okruzhenii ne nuzhen.
"""
import base64
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rdpconn import clipboard as cb
from rdpconn import config, framing

fails = []


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def pair(**kw):
    """Dva konca obschego obschestvo pamyati. Reset - chtoby shtatki ne tekli."""
    cb.MemoryClipboard.reset()
    a = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A", **kw)
    b = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B", **kw)
    return a, b


cases = [
    ("ACK", framing.build_frame(config.ACK, 0, b"")),
    ("PING", framing.build_frame(config.PING, 1, b"\x01")),
    ("CMD", framing.build_frame(config.CMD, 2, b"echo hello\n")),
    ("NOTE", framing.build_frame(config.NOTE, 3, b"\x01\x18\x50")),
    ("NAK", framing.build_frame(config.NAK, 4, bytes((config.NAK_CRC,)))),
]

print("=== 1. kadr protokola skvoz slot (bayt-v-bayt) ===")
for tag, frame_bytes in cases:
    a, b = pair()
    a.send(frame_bytes)
    got = b.receive(timeout=0.2)
    check(tag, got == frame_bytes, f"{len(frame_bytes)} B -> {len(got) if got else 0} B")
    a.close(); b.close()

print("=== 2. max payload (240 B) ===")
a, b = pair()
big = framing.build_frame(config.NOTE, 9, bytes(range(240)))
a.send(big)
got = b.receive(timeout=0.2)
check("SCREEN_FULL 240B", got == big, f"{len(big)} B")
a.close(); b.close()

print("=== 3. obratnoye napravlenio i otdelenie svoego ===")
a, b = pair()
a.send(cases[2][1])
check("B prinyal ot A", b.receive(timeout=0.2) == cases[2][1])
check("A ne chitaet svoy kadr", a.receive(timeout=0.1) is None)
b.send(cases[0][1])
check("A prinyal ot B", a.receive(timeout=0.2) == cases[0][1])
check("B ne chitaet svoy kadr", b.receive(timeout=0.1) is None)
check("peers uznany", (a.peer, b.peer) == ("B", "A"), f"a.peer={a.peer} b.peer={b.peer}")
a.close(); b.close()

print("=== 3b. napravleniya v odnom slote ne zerayut drug druga ===")
# Rad kruxa dvustoronnosti: poka A zhdet otvet, ego CMD vse eshё v slote, a
# otvet B stoit ryadom v tom zhe tekste. Esli by zapis' perezapisyvala slot
# celigom, otvet B ster by komandu A, i lyuboy povtor CMD ischez by bez sleda.
a, b = pair()
a.send(cases[2][1])          # A->B: CMD
b.send(cases[0][1])          # B->A: ACK, v etot zhe slot
text = cb.MemoryClipboard().read()
check("obe yacheyki v odnom tekste", text.count("|") == 2, repr(text)[:70])
# B prinyal A cherez _publish (_pending), a ne cherez osobyy _accept.
# Proveryaem _pending: tam dolzhen byt' kadr A.
check("B vidit A v _pending", any(b._pending),
      f"_pending={[len(p) for p in b._pending]}")
check("A->B dozhil do B", b._pending and b._pending[0] == cases[2][1],
      repr(b._pending[0] if b._pending else None))
# A vidit B cherez receive (seq 1→2).
got_ack = a.receive(timeout=0.2)
check("B->A doshel do A", got_ack == cases[0][1])
a.close(); b.close()

print("=== 3c. obe novye yaichek za odnu zapis' otdayutsya obe (ochered) ===")
# Priyomnik mog usnut' i prodremat' paky, gde obe storony uspeli zapisat'sya.
# Vyruchit tol'ko odnu iz nih - vtoraya propala by navsegda, bez kakogo-libo
# priznaka, chto ona voobshche byla.
cb.MemoryClipboard.reset()
t3 = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="C")
# U t3 _seen_seq = 0 (slot novyy). Teper' ta i tb zapolnyayut odin i tot zhe
# singleton: t3 pri pervom receive() vidit prygok seq 0→2 i razbiraet _accept.
ta = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
tb = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
ta.send(cases[2][1])
tb.send(cases[0][1])
first, second = t3.receive(timeout=0.3), t3.receive(timeout=0.3)
check(" oba kadra iz odnoi zapisi", sorted([first, second]) == sorted([cases[2][1], cases[0][1]]),
      f"{len(first or b'')}+{len(second or b'')} B")
t3.close(); ta.close(); tb.close()

print("=== 3d. restart sobesednika (nomer snova s 1) ne glushit kanal ===")
a, b = pair()
a.send(cases[2][1])
check("pervyi kadr prinyat", b.receive(timeout=0.2) == cases[2][1])
# Novyi protsess A: tot zhe id, no schyotchik zanimalis' s nachala.
a2 = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
a2.send(cases[0][1])
check("posle restarta prinyal", b.receive(timeout=0.2) == cases[0][1],
      "inache kanal stoyal by vechno posle perezapuskan odnoi storony")
a2.close(); a.close(); b.close()

print("=== 3e. reset_seen nachalo novogo sezana ===")
a, b = pair()
a.send(cases[2][1])
check("kadr prinyat", b.receive(timeout=0.2) == cases[2][1])
b.reset_seen()
a._out_seq -= 1          # tot zhe kadr s tem zhe nomerom: bez sbrosa on propuschen
a._last_out = cases[2][1]
a.send(cases[2][1])
check("posle reset_seen prinyal sнова", b.receive(timeout=0.2) == cases[2][1])
a.close(); b.close()

print("=== 3f. parallel nash zapis' ne zerayet yacheyku, vpituyu mezhdu chteniem i zapisyu ===")
# Boevaya gonka: poka A sobiraet tekst slota, B uspivaet vpisat' svoyu
# yaichek. Esli A pishet to, chto chital DO etogo, on perezapishet kadr B,
# kotornogo on ne videl, — B nikogda ne dozhdyotsya otveta. Tak na zhivom
# bufer terjalsya pervyi CMD. Store s podstavnoi zapisyu delaet gonku
# determinirovannoi.
cb.MemoryClipboard.reset()
class RacyStore:
    """Propuskaet chuzhoi send() mezhdu nashim read() i nashim write()."""
    def __init__(self):
        self.inner = cb.MemoryClipboard()
        self.fired = False
    def seq(self):
        return self.inner.seq()
    def read(self):
        text = self.inner.read()
        if not self.fired:
            self.fired = True
            b = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
            b.send(cases[0][1])   # kadr B poyavlyaetsya v slot pryamо segodnya
        return text
    def write(self, text):
        self.inner.write(text)

me = cb.ClipboardTransport(RacyStore(), peer_id="A")
me.send(cases[2][1])
slotted = cb.MemoryClipboard().read()
check("gonka nastupila", me._store.fired)
check("nasha yacheyka na meste", "|A:" in slotted, repr(slotted)[:60])
check("yacheyka B NE zater", "|B:" in slotted,
      "inache kadr B propal by bez sleda — imenno tak terlas pervaya popytka CMD")

print("=== 3g. kadr, prishedshey vo vremya nashei otpravki, ne teryaetsya ===")
# Tot zhe sluchay s drugoy storony: my dolzhny otdat' etot kadr, a ne
# vykidyt' ego iz-za podtyanutogo posle zapisi _seen_seq.
got = me.receive(timeout=0.2)
check("kadr B otdan iz _pending", got == cases[0][1], f"got={got!r}")
me.close()

print("=== 3h. chuzhaya istoriya v slote ne byvaet dlinnee nashei ===")
# Ruchnoi sluchai: v slote lezhit dlinnaya istoriya odnoi storony (staraya
# versiya koda ili pravka vruchnuyu). Perenosya e kak est, my razduvayem tekst
# slota, kotoryi drugaya storona perechityvaet pered kazhdoi zapisyu.
cb.MemoryClipboard.reset()
last = cb.HISTORY_SLOTS + 8
long_chain = "|".join(
    "OLD:%d:%s" % (n, base64.b64encode(b"x" * 8).decode("ascii"))
    for n in range(1, last + 1))
cb.MemoryClipboard().write("RDP1|" + long_chain)
me2 = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="NEW")
me2.send(cases[0][1])
slotted = cb.MemoryClipboard().read()
check("chuzhaya istoriya obrezana", slotted.count("|OLD:") <= cb.HISTORY_SLOTS,
      f"yacheyek OLD={slotted.count('|OLD:')} iz {last}, dlina={len(slotted)}")
check("naiboleye svezhie chuzhie sokhraneny", "|OLD:%d:" % last in slotted,
      "obrezat' nado starshe, a ne svezhie")
check("samye starye chuzhie vybrosheny", "|OLD:1:" not in slotted)
check("uzhe lezhavshee ne chitaetsya snova", me2.receive(timeout=0.05) is None,
      "novyy protsess ne pereigryvaet slot, v kotoryy on tolko chto voshel")
old = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="OLD")
old.send(cases[2][1])
check("novyy kadr posle starta dohodyit", me2.receive(timeout=0.2) == cases[2][1])
old.close(); me2.close()

print("=== 3i. novyy protsess ne otdaet staryy ACK kak otvet ===")
cb.MemoryClipboard.reset()
old_b = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
old_b.send(cases[0][1])
old_b.close()
fresh = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
fresh.send(cases[2][1])
check("staryy ACK ne v ocheredi", fresh.receive(timeout=0.1) is None,
      "inache seq=0 novogo tx sovpal by so starym podtverzhdeniem")
fresh.close()

print("=== 3j. neudachnoe chtenie ne sdvigaet kursor ===")
cb.MemoryClipboard.reset()


class FlakyRead:
    def __init__(self):
        self.inner = cb.MemoryClipboard()
        self.fails = 0

    def seq(self):
        return self.inner.seq()

    def read(self):
        if self.fails:
            self.fails -= 1
            raise cb.ClipboardError("busy")
        return self.inner.read()

    def write(self, text):
        self.inner.write(text)


flaky = FlakyRead()
reader = cb.ClipboardTransport(flaky, peer_id="A")
writer = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
writer.send(cases[2][1])
flaky.fails = 1
got = reader.receive(timeout=0.4)
check("kadr na meste posle zanyatogo bufera", got == cases[2][1],
      f"got={got!r}")
reader.close(); writer.close()

print("=== 3k. pachka otveta ne stirayet ACK ===")
cb.MemoryClipboard.reset()
notes = (config.EXEC_OUTPUT_LIMIT + config.MAX_PAYLOAD - 1) // config.MAX_PAYLOAD
burst = [framing.ack(1)]
for i in range(notes):
    burst.append(framing.build_frame(config.NOTE, 1, bytes([i])))
side = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
for raw in burst:
    side.send(raw)
text = cb.MemoryClipboard().read() or ""
missing = [raw for raw in burst if base64.b64encode(raw).decode("ascii") not in text]
check("ves otvet viden v slote", not missing,
      f"poteryano {len(missing)} iz {len(burst)}, slots={cb.HISTORY_SLOTS}")
side.send(framing.pong(1))
ack_b64 = base64.b64encode(burst[0]).decode("ascii")
check("sluzhebnaya yacheyka ne vytesnyaet ACK",
      ack_b64 in (cb.MemoryClipboard().read() or ""))
side.send(framing.pong(2))
check("za predelom istorii ACK uzhe ne obyazan lezhat",
      ack_b64 not in (cb.MemoryClipboard().read() or ""))
side.close()

print("=== 3l. otvet pishhetsya odnoi zapisyu ===")
cb.MemoryClipboard.reset()


class Counting(cb.MemoryClipboard):
    writes = 0

    def write(self, text):
        Counting.writes += 1
        super().write(text)


Counting.writes = 0
store = Counting()
burst = [framing.ack(1)]
for i in range(7):
    burst.append(framing.build_frame(config.NOTE, 1, bytes([i])))
writer = cb.ClipboardTransport(store, peer_id="B")
reader = cb.ClipboardTransport(store, peer_id="A")
writer.send_many(burst)
text = store.read() or ""
missing = [raw for raw in burst if base64.b64encode(raw).decode("ascii") not in text]
check("odna zapis na ves otvet", Counting.writes == 1, f"writes={Counting.writes}")
check("vse kadry v etoi zapisi", not missing, f"net {len(missing)}")
got = []
while True:
    item = reader.receive(timeout=0.05)
    if item is None:
        break
    got.append(item)
check("ACK prihodit pervym", got[:1] == burst[:1])
check("pachka doshla celikom i po poryadku", got == burst, f"got={len(got)}")
writer.close(); reader.close()

print("=== 3m. novyy otvet ne vezet yacheyki proshlogo ===")
cb.MemoryClipboard.reset()
old = [framing.ack(1), framing.build_frame(config.NOTE, 1, b"OLD-REPLY")]
new = [framing.ack(2), framing.build_frame(config.NOTE, 2, b"NEW-REPLY")]
side = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="B")
peer = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
side.send_many(old)
while peer.receive(timeout=0.05) is not None:
    pass
side.send_many(new)
text = cb.MemoryClipboard().read() or ""
old_b64 = base64.b64encode(old[1]).decode("ascii")
new_b64 = base64.b64encode(new[1]).decode("ascii")
check("staryy NOTE ne v novoi zapisi", old_b64 not in text)
check("novyy NOTE v zapisi", new_b64 in text)
got = []
while True:
    item = peer.receive(timeout=0.05)
    if item is None:
        break
    got.append(item)
check("prinyat tolko novyy otvet", got == new, f"got={got!r}")
side.close(); peer.close()

print("=== 4. chuzhaya kopiya: ne razbiraetsya, zanimaet slot, NO priyoma ne glushit ===")
a, b = pair()
cb.MemoryClipboard().write("prosto tekst iz bloknota")
check("chuzhoe ne kadr", b.receive(timeout=0.1) is None)
check("busy vzvelas", b.busy, "slot zanyat chelovekom")
check("pauzy NET (priyoma eto ne kasaetsya)", not b.paused)
# Glavnoe: nesmotrya na zanyatyy slot, kadr ot sobesednika prihodit.
a.send(cases[2][1])
check("priyom rabotaet pri zanyatom slote", b.receive(timeout=0.2) == cases[2][1])
a.close(); b.close()

a, b = pair(auto_pause=False)
cb.MemoryClipboard().write("chuzhoy tekst")
b.receive(timeout=0.1)
check("--no-auto-pause slot ne zanimaet", not b.busy and not b.paused)
a.close(); b.close()

print("=== 4b. zanyatyy slot NE blokiruet zapis': inache ACK ne otpravit ===")
# Eto ne detal' stilistiki, a jedinstvennaya nerabochaya politika. Bufer v
# RDP-pochti-vsegda zanyat poste storonnego, i zapret zapisi po busy
# oznachal by, chto priemnik ne mozhet otvetit ACK; otpratchik povtoryaet CMD,
# obmen ne dvigaetsya voobshche.
a, b = pair(pause_timeout=0.5)
cb.MemoryClipboard().write("chelovek kopiruet")
b.receive(timeout=0.1)
check("snachala zanyat", b.busy)
check("no zapis razreshena", not b.blocked,
      "busy = vezhlivost, no ne zapret protokolu")
b.send(cases[0][1])
check("ACK proshel skvoz zanyatyy slot", a.receive(timeout=0.2) == cases[0][1])
a.close(); b.close()

print("=== 4c. zanyatost' snimayetsya po taymautu (informatsiya, ne vечnyy zapret) ===")
a, b = pair(pause_timeout=0.5)
cb.MemoryClipboard().write("chelovek kopiruet")
b.receive(timeout=0.1)
check("snachala zanyat", b.busy)
time.sleep(0.5)
b.receive(timeout=0.05)
check("sam osvobodilsya po taymautu", not b.busy)
a.close(); b.close()

print("=== 5. bityy nash kadr molchit (CRC reshaet vyzyvayushchemu) ===")
a, b = pair()
cb.MemoryClipboard().write("RDP1|deadbeef:1:###not-base64###")
check("bit base64 -> None", b.receive(timeout=0.1) is None)
check("bit nash kadr NE zanimaet slot", not b.busy and not b.paused,
      "inache pomykha v slote vetchno vyzhila by obmen")
cb.MemoryClipboard().write("RDP1|deadbeef:1:AQID")  # valid base64, no ne kadr
got = b.receive(timeout=0.1)
check("valid base64 prokachaetsya naverk", got == b"\x01\x02\x03",
      "parse_frame sam reshaet pro CRC")
# Yaichek v tekste neskolko: nerazbiraemaya ne dolzhna tashit' za soboi
# razbiraemuyu, inache odna pomyekha glushila by obe napravleniya.
cb.MemoryClipboard().write("RDP1|broken|deadbeef:2:AQID")
got = b.receive(timeout=0.1)
check("sorvannaya yaichek ne glushit sosednyuyu", got == b"\x01\x02\x03",
      repr(got))
a.close(); b.close()

print("=== 6. ruchnaya pauza: send zaderzhivaetsya, receive PRODOLZHAET ===")
a, b = pair(pause_timeout=0.0)  # 0 = bez avtovozvrata, kontrol polnostyu v teste
b.pause()
a.send(cases[2][1])
check("v ruchnoy pauze kadr prinimaetsya", b.receive(timeout=0.2) == cases[2][1],
      "priyom bufer ne zatiraet, znachit ne vredit")
a.close(); b.close()

a, b = pair(pause_timeout=0.0)
b.pause()
try:
    b.send(cases[0][1])
    check("send v ruchnoy pauze padaet", False, "izklyucheniya net")
except cb.ClipboardError as exc:
    check("send v ruchnoy pauze padaet", True, str(exc)[:60])
a.close(); b.close()

print("=== 6b. tol'ko ruchnaya pauza blokiruet zapis'; busy i chtenie — net ===")
a, b = pair(pause_timeout=0.0)
check("v norme ne zanyat", not b.blocked)
b.pause()
check("paused => blocked", b.blocked and b.paused)
b.resume()
check("posle resume opyat ne zanyat", not b.blocked)
cb.MemoryClipboard().write("chelovek kopiruet")
b.receive(timeout=0.1)
check("busy, no NE blocked", b.busy and not b.blocked,
      "otvetit ACK dolzhno mozhno pri lyubom soderzhimom slota")
a.close(); b.close()

print("=== 7. avto-vozobnovlenie po taymautu ===")
a, b = pair(pause_timeout=0.1)
b.pause()
check("snachala pauza", b.paused)
time.sleep(0.2)
a.send(cases[2][1])
check("sam vyshel iz pauzy i prinyal", b.receive(timeout=0.2) == cases[2][1])
a.close(); b.close()

print("=== 8. restore: vozvrat poslednego kadra, no NE cifoi chuzhoy kopii ===")
a, b = pair(restore=True, pause_timeout=0.0)
a.send(cases[2][1])
check("B prinyal", b.receive(timeout=0.2) == cases[2][1])
a.pause()
a.resume()   # v slote nash kadr -> restore ego kladet obratno
slotted = cb.MemoryClipboard().read()
# Merge-format: svoya yaichek "A:<nomer>:<base64>", pri etom nomer vyse
# pervogo (restore publikuetsya kak novyi kadrovyi nomer, a ne povtor).
check("slot opyat nash", slotted is not None and slotted.startswith("RDP1|A:")
      and base64.b64encode(cases[2][1]).decode("ascii") in slotted,
      repr(slotted)[:40])
check("B vnov prinyal posle restore", b.receive(timeout=0.2) == cases[2][1])
a.close(); b.close()

# Glavnyy sluchay: chelovek skopiroval svoyo VO VREMYA pauzy.
a, b = pair(restore=True, pause_timeout=0.0)
a.send(cases[2][1])
b.receive(timeout=0.2)
a.pause()
cb.MemoryClipboard().write("rabochiy tekst cheloveka")
a.resume()
check("chuzhuyu kopiyu NE zater", cb.MemoryClipboard().read() == "rabochiy tekst cheloveka",
      "restore obezpechivaet cheloveka, a ne perezapisyvaet ego")
check("slot pritom opredelen kak zanyatyy", a.busy)
a.close(); b.close()

print("=== 9. razbor goryachei klavishi ===")
for spec, expect in (("ctrl+alt+f9", (0x3, 0x78)), ("Ctrl+F12", (0x2, 0x7B)),
                     ("shift+a", (0x4, 0x41)), ("win+numpad3", (0x8, 0x63))):
    try:
        got = cb.Hotkey.parse(spec)
        check(f"parse {spec}", got == expect, f"{got} vs {expect}")
    except cb.ClipboardError as exc:
        check(f"parse {spec}", False, str(exc))
for bad in ("ctrl+plus", "medial", "f99", ""):
    try:
        cb.Hotkey.parse(bad)
        check(f"otkaz na {bad!r}", False, "prinyalos kak validnoe")
    except cb.ClipboardError:
        check(f"otkaz na {bad!r}", True)

print("=== 10. file-slot mezhdu dvumya transportami ===")
path = os.path.join(ROOT, "tmp", "clip_slot.txt")
os.makedirs(os.path.dirname(path), exist_ok=True)
if os.path.exists(path):
    os.remove(path)
a = cb.ClipboardTransport(cb.FileClipboard(path), peer_id="A")
b = cb.ClipboardTransport(cb.FileClipboard(path), peer_id="B")
a.send(cases[2][1])
check("cherez fail", b.receive(timeout=0.3) == cases[2][1])
b.send(cases[0][1])
check("obratno cherez fail", a.receive(timeout=0.3) == cases[0][1])
a.close(); b.close()
os.remove(path)

print("=== 11. tikhiy povtor — novaya yacheika teh zhe baitov ===")
cb.MemoryClipboard.reset()
a = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
a.send(cases[2][1])
first_cells = a._parse(cb.MemoryClipboard._text)["A"]
a._nudge_mine = a._my_history
a._nudge_at = 0
a._maybe_nudge()
second_cells = a._parse(cb.MemoryClipboard._text)["A"]
check("novyi nomer yacheiki", second_cells[-1][0] != first_cells[-1][0],
      str(second_cells))
check("payload tot zhe", second_cells[-1][1] == first_cells[-1][1])
check("povtor sam sebya ne zakazyvaet", a._nudge_at is None)
kept = cb.MemoryClipboard._text
a._pending.append(b"already")
a._nudge_mine = a._my_history
a._nudge_at = 0
a._maybe_nudge()
check("otvet v ocheredi — povtor ne pishem", cb.MemoryClipboard._text == kept)
check("povtor otlozhen, a ne otmenen",
      a._nudge_at is not None and a._nudge_mine is a._my_history)
a._pending.clear()
a._nudge_at = 0
a._maybe_nudge()
check("posle ocheredi povtor pishetsya", cb.MemoryClipboard._text != kept)
a.close()

print("=== 12. nomer slota operezhaet tekst ===")


class LateStore:
    def __init__(self):
        self.seq_n = 1
        self.text = None
        self.pending = None
        self.delay = 0

    def seq(self):
        return self.seq_n

    def read(self):
        if self.delay > 0:
            self.delay -= 1
            return self.text
        if self.pending is not None:
            self.text = self.pending
            self.pending = None
        return self.text

    def write(self, text):
        self.text = text
        self.seq_n += 1


late = LateStore()
rx = cb.ClipboardTransport(late, peer_id="B", auto_pause=False)
payload = cases[0][1]
late.seq_n = 8
late.delay = 1
late.pending = "RDP1|A:7:" + base64.b64encode(payload).decode("ascii")
got = rx.receive(2.5)
check("kadr doshel so vtorogo chteniya", got == payload, repr(got)[:80] if got else "None")
rx.close()

print("=== 13. snimok krupnee 16 KB rezhetsya ===")
one = cb.snapshot_pieces(b"x" * cb.SNAPSHOT_MAX)
check("16 KB — odna zapis", len(one) == 1 and len(one[0]) == cb.SNAPSHOT_MAX)
four = cb.snapshot_pieces(b"y" * (cb.SNAPSHOT_MAX * 4))
check("64 KB — chetyre zapisi",
      [len(part) for part in four] == [cb.SNAPSHOT_MAX] * 4)
check("kuski skleivayutsya", b"".join(four) == b"y" * (cb.SNAPSHOT_MAX * 4))
cb.MemoryClipboard.reset()
big = cb.ClipboardTransport(cb.MemoryClipboard(), peer_id="A")
try:
    big._publish(b"z" * (cb.SNAPSHOT_MAX + 1))
    check("snimok krupnee predela ne pishhetsya", False, "zapisalas")
except ValueError:
    check("snimok krupnee predela ne pishhetsya", True)
big.close()

print()
print("RESULT:", "FAIL\n  - " + "\n  - ".join(fails) if fails else "ALL GREEN")
sys.exit(1 if fails else 0)
