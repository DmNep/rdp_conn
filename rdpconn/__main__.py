"""Точка входа: `python -m rdpconn tx|rx`.

Эквивалентно `python -m rdpconn.tx`, но одна команда и общий хелп по каналу.
"""

from __future__ import annotations

import sys

from . import gate, rx, tx

USAGE = """канал управления через буфер обмена RDP

    python -m rdpconn tx --peer-id A "hostname"     отправить команду
    python -m rdpconn rx --peer-id B --exec         принять и исполнить
    python -m rdpconn gate listen --peer-id B      прокси там, где нет интернета
    python -m rdpconn gate dial --peer-id A        выход в интернет на этой машине
    python -m rdpconn tx --help                     все ключи отправителя
"""


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    mode, rest = argv[0], argv[1:]
    if mode == "tx":
        return tx.main(rest)
    if mode == "rx":
        return rx.main(rest)
    if mode == "gate":
        return gate.main(rest)
    print(f"не понимаю {mode!r}, нужно tx, rx или gate\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
