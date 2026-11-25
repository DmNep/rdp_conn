"""Точка входа: `python -m rdpconn tx|rx`.

Эквивалентно `python -m rdpconn.tx`, но одна команда и общий хелп по каналу.
"""

from __future__ import annotations

import sys

from . import coder, ends, gate, monitor, rx, tx

USAGE = """канал управления через буфер обмена RDP

    python -m rdpconn tx --peer-id A "hostname"     отправить команду
    python -m rdpconn rx --peer-id B --exec         принять и исполнить
    python -m rdpconn gate listen --peer-id B      прокси там, где нет интернета
    python -m rdpconn gate dial --peer-id A        выход в интернет на этой машине
    python -m rdpconn coder host --peer-id A       текст локальной модели на этой машине
    python -m rdpconn coder guest --peer-id B      тот же текст для Cline в сессии
    python -m rdpconn host --feed model            то же, плюс страница статистики
    python -m rdpconn guest --feed model           гуест модели, одна роль на запуск
    python -m rdpconn host --feed net              выход в интернет на этой машине
    python -m rdpconn guest --feed net             прокси только на 127.0.0.1 в сессии
    python -m rdpconn monitor                      смотреть слот с этой машины, не записывая его
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
    if mode == "coder":
        return coder.main(rest)
    if mode == "monitor":
        return monitor.main(rest)
    if mode == "host":
        return ends.main("host", rest)
    if mode == "guest":
        return ends.main("guest", rest)
    print(f"не понимаю {mode!r}, нужно tx, rx, gate, coder, host, guest или monitor\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
