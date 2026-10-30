"""Skvoznoy progon dvustoronnego kanala cherez CLI (dva protsessa, odin slot).

Chto dokazyvaet — to, chego ne vidno v modulnykh testakh:

  - tx i rx deystvitelno razgovarivayut cherez odin i tot zhe slot: CMD -> ACK
    -> NOTE s otvetom, i vse tri kadra ne zatirayut drug druga;
  - rx --exec ispolnyaet komandu i vozvrat ee vyvoda dohodit do tx;
  - PING/PONG rabotayut mezhdu protsessami (heartbeat zhivoy, a ne tol'ko
    vnutri odnogo ob'ekta);
  - ostanovka rx ne veshaet tx: on soobshchaet o tishine i zavershaetsya.

Slot — faylovyy (--slot file:PATH), poetomu Windows-API i vtoraya mashina ne
nuzhny: logika ta zhe, chto v boevom buferе.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AM = ROOT
sys.path.insert(0, ROOT)
from rdpconn import rx as rx_mod
PY = sys.executable
ENV = dict(os.environ, PYTHONPATH=os.pathsep.join([ROOT, AM]), PYTHONIOENCODING="utf-8")

fails = []


def check(name, ok, detail=""):
    print(f"  {name}: {'OK' if ok else 'FAIL'}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def run(args, timeout=25.0):
    """Zapustit modul rdpconn i vernut (stdout, stderr, kod)."""
    proc = subprocess.run(
        [PY, "-m", args[0]] + args[1:],
        cwd=AM, env=ENV, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.stdout, proc.stderr, proc.returncode


def start(args):
    """Запуск долгоживущего процесса прогона.

    stdin — обязательно труба, а не наследование: --repl читает ввод, а
    закрытый родительский ввод даёт мгновенный EOF, и tx завершался бы ещё до
    первого PING. Тогда проверка обмена PING/PONG зависела бы не от канала, а
    от того, из какого терминала запустили сами тест.
    """
    return subprocess.Popen(
        [PY, "-m", args[0]] + args[1:],
        cwd=AM, env=ENV, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )


tmp = tempfile.mkdtemp(prefix="xconn_duplex_")
slot = os.path.join(tmp, "slot.txt")
print(f"slot: {slot}")

print("=== 1. CMD -> ACK -> NOTE: tri kadra v odnom slote ===")
rx = start(["rdpconn.rx", "--slot", f"file:{slot}",
            "--peer-id", "B", "--ping", "0", "--duration", "20"])
time.sleep(1.5)
out, err, code = run(["rdpconn.tx", "--slot",
                      f"file:{slot}", "--peer-id", "A", "--ping", "0",
                      "--reply-wait", "1.5", "echo privet"])
rx.terminate()
rx_out, rx_err = rx.communicate(timeout=10)
print("  --- tx ---")
print("  " + "\n  ".join(out.strip().splitlines()[-8:]))
check("tx uspeshen", code == 0, f"code={code} stderr={err.strip()[:80]}")
check("CMD otpravlen", "CMD seq=" in out)
check("ACK prinyat", "<< ACK" in out)
check("NOTE-otvet prinyat", "<< NOTE" in out and "privet" in out,
      "otvet sobosednika ne doshel do otpravitelya")
print("  --- rx ---")
print("  " + "\n  ".join(rx_out.strip().splitlines()[-8:]))
check("rx prinyal CMD", "CMD" in rx_out)
check("rx otvetil ACK", ">> ACK" in rx_out)

print("=== 2. --exec: vyvod komandy vozvratilsya otpravitelyu ===")
slot2 = os.path.join(tmp, "slot2.txt")
rx = start(["rdpconn.rx", "--slot", f"file:{slot2}",
            "--peer-id", "B", "--ping", "0", "--exec", "--duration", "20"])
time.sleep(1.5)
out, err, code = run(["rdpconn.tx", "--slot",
                      f"file:{slot2}", "--peer-id", "A", "--ping", "0",
                      "--reply-wait", "2.0", "echo hello_from_b"])
rx.terminate()
rx_out, _ = rx.communicate(timeout=10)
check("komanda ispolnena i vyvod doshel", "hello_from_b" in out,
      f"tx: {[l for l in out.splitlines() if 'NOTE' in l]}")
check("rx soobshchil o NOTE", ">> NOTE" in rx_out)

print("=== 2b. cmd /c ne prileplyaet kavychku k poslednemu slovu ===")
echo = rx_mod._run_command("cmd /c echo hello_exact", 10)
echo_text = echo.stdout.decode("cp866", errors="replace")
check("echo bez hvoska-kavychki", echo_text.strip() == "hello_exact", repr(echo_text))
env = rx_mod._run_command("cmd /c set", 10)
env_text = env.stdout.decode("cp866", errors="replace")
check("set vernul okruzhenie", env.returncode == 0 and "SystemRoot=" in env_text,
      env_text[:80])

print("=== 3. bez --exec: NOTE-eho (vidno, chto doehало) ===")
slot3 = os.path.join(tmp, "slot3.txt")
rx = start(["rdpconn.rx", "--slot", f"file:{slot3}",
            "--peer-id", "B", "--ping", "0", "--duration", "20"])
time.sleep(1.5)
out, err, code = run(["rdpconn.tx", "--slot",
                      f"file:{slot3}", "--peer-id", "A", "--ping", "0",
                      "--reply-wait", "1.5", "kto ty"])
rx.terminate()
rx_out, _ = rx.communicate(timeout=10)
check("eho doshlo", "kto ty" in out, "eho pokazyvaet, chto real'no doehаlo")
check("rx pometil eho", "эхо" in rx_out, f"rx: {rx_out.strip().splitlines()[-3:]}")

print("=== 4. heartbeat mezhdu protsessami: PING -> PONG ===")
slot4 = os.path.join(tmp, "slot4.txt")
rx = start(["rdpconn.rx", "--slot", f"file:{slot4}",
            "--peer-id", "B", "--ping", "0.5", "--duration", "25"])
time.sleep(1.2)
tx = start(["rdpconn.tx", "--slot", f"file:{slot4}",
            "--peer-id", "A", "--ping", "0.5", "--reply-wait", "1.0", "--repl"])
time.sleep(4.0)
tx.terminate()
rx.terminate()
tx_out, _ = tx.communicate(timeout=10)
rx_out, _ = rx.communicate(timeout=10)
check("tx posylal PING", ">> PING" in tx_out, f"tx: {tx_out.strip().splitlines()[-4:]}")
check("rx otvetil PONG", ">> PONG" in rx_out)
check("tx uvidel PONG", "<< PONG" in tx_out, "inache tx schital by rx mertvym")

print("=== 5. ostanovka rx ne veshaet tx: on soobshchaet o tishine ===")
slot5 = os.path.join(tmp, "slot5.txt")
# Nikogo na toy storone: tx obyazan zakonchit'sya sam, a ne zavisnut'.
t0 = time.monotonic()
out, err, code = run(["rdpconn.tx", "--slot",
                      f"file:{slot5}", "--peer-id", "A", "--ping", "0",
                      "--wait", "0.4", "--retries", "2", "nikogo net"])
spent = time.monotonic() - t0
check("tx zavershilsya sam", code == 1, f"code={code} za {spent:.1f} s")
check("soobshchil o tishine", "тишина" in out or "тишина" in err,
      "chelovek dolzhen znat, chto otveta net")
check("ne zavis", spent < 15, f"{spent:.1f} s")

print("=== 6. kirillitsa: bufernyi kanal neset ee baytami cp866 ===")
# Boevaya nahodka: A otnosila 'уцкуцк', a B videl b'??????' — eshche do
# otpravki. cp437 kirillitsy ne soderzhit sovsem, a errors='replace' stavil
# voprositel'nye znaki. Eho — samaya chistaya proverka: B vozvraschaet te zhe
# bayty, kotorye poluchil, tak chto esli A chitaet ishodnyy tekst, kodek
# odinakovy s obeih storon i posadka ne v konsoli.
slot6 = os.path.join(tmp, "slot6.txt")
rx = start(["rdpconn.rx", "--slot", f"file:{slot6}",
            "--peer-id", "B", "--ping", "0", "--duration", "20"])
time.sleep(1.5)
out, err, code = run(["rdpconn.tx", "--slot",
                      f"file:{slot6}", "--peer-id", "A", "--ping", "0",
                      "--reply-wait", "1.5", "уцкуцк"])
rx.terminate()
rx_out, _ = rx.communicate(timeout=10)
check("eho doshlo kirillicei", "уцкуцк" in out,
      f"tx: {[l for l in out.splitlines() if 'NOTE' in l]}")
check("rx rashifroval komandu, a ne '??????'", "уцкуцк" in rx_out,
      f"rx: {[l for l in rx_out.splitlines() if 'CMD' in l]}")

print("=== 7. komanda dlinnee 240 bait sobiraetsya i ne padaet ===")
slot7 = os.path.join(tmp, "slot7.txt")
long_cmd = "echo " + ("Q" * 260)
rx = start(["rdpconn.rx", "--slot", f"file:{slot7}",
            "--peer-id", "B", "--ping", "0", "--exec", "--duration", "30"])
time.sleep(1.5)
out, err, code = run(["rdpconn.tx", "--slot",
                      f"file:{slot7}", "--peer-id", "A", "--ping", "0",
                      "--wait", "5", "--reply-wait", "2", "--retries", "2",
                      long_cmd], timeout=40)
rx.terminate()
rx_out, _ = rx.communicate(timeout=10)
check("tx ne upal i vyshel 0", code == 0 and "ValueError" not in (out + err),
      f"code={code} err={err.strip()[:120]}")
check("s pervoi popytki", "(попытка 2)" not in out, "povtor znachit, chto otvet zatjor promezhutochnyi ACK")
check("v zhurnale vidny kuski", "часть 1/" in out and "часть 2/" in out,
      f"tx: {[l for l in out.splitlines() if 'CMD' in l]}")
check("vyvod sobran celikom", out.count("Q") >= 260, f"Q={out.count('Q')}")
check("rx prinyal kuski, a ne syroy zagolovok kak komandu",
      "часть 1/" in rx_out and "часть 2/" in rx_out,
      f"rx: {[l for l in rx_out.splitlines() if 'CMD' in l or 'часть' in l]}")

shutil.rmtree(tmp, ignore_errors=True)
print()
print("RESULT:", "FAIL\n  - " + "\n  - ".join(fails) if fails else "ALL GREEN")
sys.exit(1 if fails else 0)
