"""Хост снаружи сессии и гуест внутри. Один модуль, режим задаёт запуск.

Модель: хост говорит с локальным сервером, гуест — с Cline построчным JSON.
Интернет: хост выходит в сеть, гуест слушает только 127.0.0.1 и никого
больше. Системный прокси не включается. Страница статистики — на хосте.
"""

from __future__ import annotations

import argparse
import sys
import threading

from . import app, coder, fileshake, gate, monitor
from .statcell import FEED_MODEL, FEED_NET, PAGE, Picture, attach_guest

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rdpconn host|guest",
        description="Один конец канала. Запуск — либо модель, либо интернет, не оба.",
    )
    p.add_argument("--feed", choices=("model", "net"), required=True,
                   help="model — локальная модель для Cline; net — интернет выбранным программам")
    app.add_channel_args(p)
    for action in p._actions:
        if action.dest == "peer_id":
            action.help = "метка конца в слоте. Пусто — A на хосте и B на гуесте"
    p.add_argument("--upstream", default="http://127.0.0.1:8080/v1",
                   help="хост, модель: OpenAI-совместимый адрес на этой машине")
    p.add_argument("--model", default="",
                   help="имя модели в запросе; пусто — как решит сервер")
    p.add_argument("--once", default="",
                   help="гуест, модель: один запрос из JSON-файла и выход")
    p.add_argument("--cells", type=int, default=coder.CELLS,
                   help=f"ячеек данных в снимке модели (по умолчанию {coder.CELLS})")
    p.add_argument("--proxy", default="127.0.0.1:8079",
                   help="гуест, интернет: локальный прокси (только 127.0.0.1)")
    p.add_argument("--monitor", default=None,
                   help="хост: страница, по умолчанию 127.0.0.1:8078. Пусто — не поднимать")
    p.add_argument("--drop", default="",
                   help="папка файлового канала F1: DROP/out/* уходит собеседнику, "
                        "входящие ложатся в DROP/in/. Пусто — выключено "
                        "(только feed=model: у gate.py нет точки простоя для проверки)")
    return p


def _split(spec: str, default_port: int) -> tuple[str, int]:
    host, _, port_s = spec.rpartition(":")
    return (host or "127.0.0.1"), int(port_s or default_port)


def main(role: str, argv=None) -> int:
    args = build_parser().parse_args(argv)
    if role not in ("host", "guest"):
        app.warn(f"не понимаю роль {role!r}")
        return 2
    if not args.peer_id:
        args.peer_id = "A" if role == "host" else "B"
    if role == "guest" and args.monitor:
        app.warn("страница только на хосте, на гуесте её не поднимаю")
        args.monitor = ""
    if args.drop and args.feed == "net":
        app.warn("папка (--drop) пока только для feed=model, игнорирую")
        args.drop = ""
    if role == "guest" and not coder.claim_guest():
        app.warn("кодер уже в сессии")
        return 0
    if role == "guest":
        coder.watch_parent()
    try:
        channel = app.open_channel(args)
    except app.UsageError as exc:
        app.warn(str(exc))
        return 2
    talk = app.warn if role == "guest" else app.say
    talk(f"канал: {channel.label}")
    app.peer_hint(args)
    if role == "guest":
        attach_guest(channel.transport, FEED_MODEL if args.feed == "model" else FEED_NET)
    else:
        _host_page(channel.transport, args)
    try:
        if args.feed == "model":
            return _model(role, args, channel, talk)
        return _net(role, args, channel, talk)
    except KeyboardInterrupt:
        app.say("")
    finally:
        channel.close()
    return 0


def _host_page(transport, args) -> None:
    spec = "127.0.0.1:8078" if args.monitor is None else args.monitor
    picture = Picture(args.feed)
    transport.picture = picture
    if not spec:
        return
    host, port = _split(spec, 8078)
    if host not in _LOOPBACK:
        app.warn(f"!! страницу слушаю только на localhost, не на {host}")
        host = "127.0.0.1"
    monitor.serve(picture, host, port, page=PAGE)


def _model(role: str, args, channel, talk) -> int:
    wait = args.wait
    store = getattr(channel.transport, "_store", None)
    if store is not None and store.__class__.__name__ == "WindowsClipboard" and wait < 12:
        wait = 12
        talk(".. жду снимок до 12 с")
    link = coder.Link(channel.transport, cells=max(1, args.cells), reply_wait=wait)
    watcher = fileshake.DropWatcher(channel.transport, args.drop) if args.drop else None
    if watcher is not None:
        talk(f".. файловая папка: {args.drop}")
    if role == "host":
        app.say(f"хост, модель {args.upstream}")
        if watcher is not None:
            # Простой между запросами — единственная точка, где read_request()
            # зовёт on_tick (coder.py, только пока chunks пуст): фоновый
            # beat-поток уже остановлен к этому моменту, писать в транспорт
            # кроме нас тут некому.
            link.on_tick = watcher.tick

        def complete(messages):
            yield from coder.iter_llama(args.upstream, args.model, messages, timeout=max(wait, 30))

        coder.serve(link, complete)
        return 0
    if watcher is None:
        return coder.guest_loop(link, args.once or None)
    return _guest_with_drop(link, args.once or None, watcher)


def _guest_with_drop(link, once_path, watcher) -> int:
    """Гость: idle-время — блокирующее чтение stdin, транспорт тогда никто не
    трогает вовсе. Фоновый поток — единственный способ туда заглянуть; lock не
    даёт ему вмешаться, пока guest_loop() сам разбирает активный запрос."""
    lock = threading.Lock()
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            # tick() сам пишет в транспорт (FILE_READY, сама передача) —
            # лочить только receive() мало, иначе run_locked() в guest_loop()
            # может начать запрос прямо посреди уже идущей файловой передачи.
            if lock.acquire(blocking=False):
                try:
                    raw = link.transport.receive(0.2)
                    watcher.tick(raw)
                finally:
                    lock.release()
                if raw is not None:
                    continue
            stop.wait(3.0)

    threading.Thread(target=poll, name="rdpconn-drop", daemon=True).start()
    try:
        return coder.guest_loop(link, once_path, file_lock=lock)
    finally:
        stop.set()


def _net(role: str, args, channel, talk) -> int:
    wait = args.wait
    store = getattr(channel.transport, "_store", None)
    if store is not None and store.__class__.__name__ == "WindowsClipboard" and wait < 8:
        wait = 8
        talk(".. жду ответ шлюза до 8 с")
    stop = threading.Event()
    if role == "host":
        app.say("хост, интернет: выход отсюда, в сессию его не раздаю")
        gate.dial_loop(channel.transport, stop, wait, None)
        return 0
    host, port = _split(args.proxy, 8079)
    if host not in _LOOPBACK:
        app.warn(f"прокси {host} снаружи сессии не слушаю, только 127.0.0.1")
        return 2
    app.warn(f"гуест, интернет: прокси {host}:{port}, системный прокси не трогаю")
    gate.listen_loop(channel.transport, host, port, stop, wait, None)
    return 0


if __name__ == "__main__":
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit(main(role, sys.argv[2:]))
