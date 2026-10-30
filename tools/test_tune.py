"""Замер паузы повтора: предел, запас 33%, простой без живого собеседника.

Скрипт, не pytest. Живой буфер здесь не открывается: проба подставлена.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rdpconn import clipboard as cb
from rdpconn import config, framing, tune

_failed = 0


def check(name, ok, detail="") -> None:
    global _failed
    if ok:
        print(f"  {name}: OK  {detail}".rstrip())
    else:
        _failed += 1
        print(f"  {name}: FAIL  {detail}".rstrip())


def main() -> int:
    saved = cb.NUDGE_AFTER_S
    try:
        print("=== 1. 0.7 s x 1.33 = 0.94 s ===")
        check("запас от 0.70", tune.operating(0.7) == 0.94, str(tune.operating(0.7)))
        check("запас от 0.80", tune.operating(0.8) == 1.07, str(tune.operating(0.8)))

        print("=== 2. poisk ostanavlivaetsya na pervom nenadeznom shage ===")
        seen = []

        def probe(delay):
            seen.append(round(delay, 2))
            return "ok" if delay >= 0.7 else "fail"

        limit, peer = tune.search(probe)
        check("predel 0.70", limit == 0.7 and peer, str(limit))
        check("0.60 probe byl", 0.6 in seen)
        check("0.50 uzhe ne probuem", 0.5 not in seen, str(seen))

        print("=== 3. bystryy uhod ne dokazyvaet predel ===")

        def only_fast(delay):
            return "ok" if delay >= 1.0 else "fast"

        limit, peer = tune.search(only_fast)
        check("ostayomsya na 1.00", limit == 1.0 and peer, str(limit))

        print("=== 3b. otvet do povtora ne udlinyaet pauzu ===")
        climbed = []

        def always_fast(delay):
            climbed.append(round(delay, 2))
            return "fast"

        limit, peer = tune.search(always_fast)
        check("net shaga vverh", limit is None and peer and max(climbed) <= 1.0, str(climbed))

        print("=== 3c. chuzhoy kadr v ocheredi ostayotsya baitami ===")

        class Fake:
            def __init__(self):
                self._pending = []
                self._nudge_at = None
                self._nudge_mine = None

            def send(self, raw):
                frame = framing.parse_frame(raw)
                self._pending.append(framing.build_frame(config.NOTE, frame.seq, b"x"))
                self._pending.append(framing.pong(frame.seq))

            def receive(self, timeout):
                if self._pending:
                    return self._pending.pop(0)
                return None

        fake = Fake()
        first = tune.sample(fake, 1.0, 7)
        second = tune.sample(fake, 1.0, 8)
        check("dve proby ne padayut", first == "fast" and second == "fast", f"{first} {second}")
        check("v ocheredi baity", all(isinstance(item, (bytes, bytearray)) for item in fake._pending))

        print("=== 4. net otveta — predela net ===")
        limit, peer = tune.search(lambda delay: "fail")
        check("net predela", limit is None and not peer)

        print("=== 5. medlennee 1 s — shag vverh ===")
        limit, peer = tune.search(lambda delay: "ok" if delay >= 1.2 else "fail")
        check("predel 1.20", limit == 1.2 and peer, str(limit))

        print("=== 6. oshibka udlinyaet pauzu, prostoy bez kadra ne pishet ===")
        pace = tune.Pace()
        pace.limit = 0.7
        pace.apply(tune.operating(0.7))
        pace.relax()
        check("posle oshibki 1.07", pace.delay == tune.operating(0.8), str(pace.delay))

        class Boom:
            def send(self, raw):
                raise AssertionError("zapis v pustuyu sessiyu")

        pace._last_work = 0.0
        pace._next_idle = 0.0
        pace._last_seen = 0.0
        pace.maybe_idle(Boom())
        check("bez sobesednika ne pishem", pace.delay == tune.operating(0.8))
    finally:
        cb.NUDGE_AFTER_S = saved
        tune._pace = None

    print("RESULT:", "ALL GREEN" if _failed == 0 else f"{_failed} FAILED")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
