"""Транспорт поверх буфера обмена Windows.

Зачем именно буфер: в RDP-среде, где проверялся канал, не работает ни одно
плечо звука (на одной стороне захват отдаёт 0 устройств из 19, на другой
политика запрещает запись), а сеть разрешает A->B только порт 3389, то есть сам
RDP. Буфер обмена при этом живёт внутри того же соединения и не требует ни
открытых портов, ни звука, ни прав администратора.

Контракт: send(bytes) и receive(timeout) -> bytes | None. Возвращаются сырые
байты кадра, а их разбором и решением про NAK занимается вызывающий
(rdpconn.app). Транспорт про протокол ничего не знает — отсюда возможность
проверить его целиком, не поднимая вторую машину.

Две вещи буфер обмена делает неудобными, и обе закрыты явно.

Слот один и глобальный: любое приложение, скопировавшее текст, затирает наш
кадр. Спасает маркер RDP1|<id>:<номер>:<base64>[|...]: текст без него — чужая
копия, она просто не разбирается и молча отбрасывается. Ячеек в тексте может
быть несколько (по одной на каждую сторону), поэтому ответ собеседника не
затирает его же команду — направления живут в одном буфере независимо.

Версия формата зашита в сам префикс ("RDP1"), а не в поле внутри ячейки:
расхождение версий обязано дать пустой разбор, а не неверно прочитанный кадр.
Смешивать концы разных версий нельзя — обмен не состоится молча.

Обмен мешает работе: наша запись затирает то, что копирует человек. Для этого
введены два уровня защиты.

1. Занятость (busy): если приёмник видит человеческую копию, он это
   замечает и говорит о ней вслух, но обмен не останавливает — ни приём, ни
   ответ. Остановка ответа означала бы тупик: буфер в RDP-сессии почти всегда
   занят чем-то посторонним, приёмник не смог бы отправить подтверждение,
   отправчик ретранслировал бы кадр и обмен не продвинулся бы никогда.
   Отдать буфер программе целиком можно явно — пауза по --hotkey.

2. Пауза (paused, --hotkey): ручное отключение обмена клавишей. В паузе
   запрещается и запись, и приём: буфер полностью отдаётся человеку.
   Повторное нажатие возобновляет обмен. --restore кладёт последний наш
   кадр обратно в слот; если за время паузы человек скопировал своё — его
   текст не затирается.

Человеческая копия и пауза — разные вещи: busy решает мелкую коллизию,
paused решает крупную. busy снимается автоматически по таймауту, paused —
только клавишей или тоже по таймауту (--pause-timeout).

Хранилище отслоено (WindowsClipboard / MemoryClipboard / FileClipboard),
чтобы вся логика — маркер, фильтрация своих кадров, занятость, пауза,
restore — проверялась без Windows-API и без второй машины.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import os
import secrets
import threading
import time

# Маркер нашего кадра и версия формата в одном токене. Текст слота без него —
# чужая копия. Кадр старого формата (XCONN1) здесь не разберётся и будет принят
# за человеческую копию: это намеренно, молча смешивать концы разных версий
# нельзя, а занятый слот хотя бы виден человеку.
PREFIX = "RDP1"
SEPARATOR = "|"
# Разделитель полей внутри ячейки. base64 не содержит ни ':', ни '|', а id
# стороны (hex или короткая метка) не должен содержать ':' — иначе разбор
# ячейки "id:n:b64" станет неоднозначным.
FIELD = ":"

# Сколько последних ячеек одной стороны остаётся в тексте слота. Это не
# косметика, а условие двусторонности: приёмник обязан подтвердить CMD и тут
# же вернуть вывод, то есть записать два кадра подряд. При единственной
# ячейке на сторону вторая запись стёрла бы первую, не дав собеседнику её
# увидеть — ACK терялся бы в большинстве обменов. Три ячейки хватает с
# запасом (ACK + пара NOTE) и держат текст слота в пределах ~1 КБ.
HISTORY_SLOTS = 3

# По умолчанию: Ctrl+Alt+F9. Дальше от стандартных связок Word/Excel/терминалов.
DEFAULT_HOTKEY = "ctrl+alt+f9"

# Опрос буфера. 50 мс — компромисс: ниже порога ощущаемой задержки обмена
# (кадр всё равно собирается дольше), но неBusyWait, греющий CPU.
POLL_S = 0.05

# Сколько ждать снятия паузы в send(). Столько же, сколько человек способен
# искать в буфере нужную строку, не считая обмен мёртвым.
SEND_WAIT_S = 30.0

# Сколько раз пересобрать текст слота, если собеседник вписался между нашим
# чтением и записью. Три — не расчёт, а компромисс: коллизия двух записей в
# буфер редка и одиночна, поэтому вторая попытка почти всегда последняя, а
# больше — значит задерживать кадр ради случая, который к четвёртой попытке
# уже не расходится. После исчерпания пишем как есть: потерять свой кадр
# хуже, чем на миг затереть чужой, — ретрансляция протокола это лечит.
PUBLISH_TRIES = 3


class ClipboardError(RuntimeError):
    """Буфер недоступен или обмен прерван паузой."""


def _gle() -> int:
    """Код последней ошибки Windows (ctypes.get_last_error без use_last_error пуст)."""
    try:
        return ctypes.windll.kernel32.GetLastError()
    except (AttributeError, OSError):
        return 0


# --- хранилища ---------------------------------------------------------------


class WindowsClipboard:
    """Настоящий буфер обмена Windows через CF_UNICODETEXT.

    Только текст: CF_UNICODETEXT выбран потому, что он переживает редирект
    RDP и совместим с любым приложением, а произвольный формат (RegisterClipboardFormat)
    через сессию не проходит. Отсюда base64 в маркере.
    """

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    _OPEN_RETRIES = 12
    _OPEN_WAIT_S = 0.02

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32  # требует Windows
        self._kernel32 = ctypes.windll.kernel32
        # Без явных типов ctypes урежет указатели до int на 64 битах:
        # GlobalLock вернёт мусор, а SetClipboardData уронит процесс.
        u, k = self._user32, self._kernel32
        u.OpenClipboard.argtypes = [ctypes.c_void_p]
        u.EmptyClipboard.restype = ctypes.c_bool
        u.CloseClipboard.restype = ctypes.c_bool
        u.IsClipboardFormatAvailable.restype = ctypes.c_bool
        u.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        u.GetClipboardSequenceNumber.restype = ctypes.c_uint
        u.SetClipboardData.restype = ctypes.c_void_p
        u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        u.GetClipboardData.restype = ctypes.c_void_p
        u.GetClipboardData.argtypes = [ctypes.c_uint]
        k.GlobalAlloc.restype = ctypes.c_void_p
        k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = [ctypes.c_void_p]
        k.GlobalUnlock.argtypes = [ctypes.c_void_p]

    def seq(self) -> int:
        """Номер изменения буфера: растёт на любую запись, нашу или чужую.

        Дешевле, чем читать содержимое, поэтому поллинг смотрит сюда, а
        содержимое берётся только когда номер сдвинулся.
        """
        return self._user32.GetClipboardSequenceNumber()

    def read(self):
        if not self._user32.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
            return None
        if not self._open():
            return None
        try:
            handle = self._user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                return None
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                return ctypes.wstring_at(pointer)
            finally:
                self._kernel32.GlobalUnlock(handle)
        finally:
            self._user32.CloseClipboard()

    def write(self, text: str) -> None:
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = self._kernel32.GlobalAlloc(self.GMEM_MOVEABLE, len(data))
        if not handle:
            raise ClipboardError("GlobalAlloc не вернул блок (буфер переполнен?)")
        pointer = self._kernel32.GlobalLock(handle)
        if not pointer:
            self._kernel32.GlobalFree(handle)
            raise ClipboardError("GlobalLock не удалось")
        ctypes.memmove(pointer, data, len(data))
        self._kernel32.GlobalUnlock(handle)

        if not self._open():
            self._kernel32.GlobalFree(handle)
            raise ClipboardError("OpenClipboard не открылся: буфер занят другим приложением")
        try:
            self._user32.EmptyClipboard()
            # После успешной SetClipboardData владеет блоком буфер обмена:
            # GlobalFree здесь означал бы двойное освобождение.
            if not self._user32.SetClipboardData(self.CF_UNICODETEXT, handle):
                raise ClipboardError(f"SetClipboardData не удалось (gle={_gle()})")
        finally:
            self._user32.CloseClipboard()

    def _open(self) -> bool:
        """Открыть с повторами: буфер держится тем, кто копирует, миллисекунды."""
        for _ in range(self._OPEN_RETRIES):
            if self._user32.OpenClipboard(None):
                return True
            time.sleep(self._OPEN_WAIT_S)
        return False


class MemoryClipboard:
    """Общий в памяти слот: два конца обмена в одном процессе (тесты)."""

    _lock = threading.Lock()
    _text = None
    _seq = 0

    def seq(self) -> int:
        return MemoryClipboard._seq

    def read(self):
        with MemoryClipboard._lock:
            return MemoryClipboard._text

    def write(self, text: str) -> None:
        with MemoryClipboard._lock:
            MemoryClipboard._text = text
            MemoryClipboard._seq += 1

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._text = None
            cls._seq += 1


class FileClipboard:
    """Слот в файле: обмен между процессами там, где нет Windows-API (CI, Linux).

    Это не замена боевому варианту, а средство проверить полный цикл
    tx<->rx двумя процессами на одной машине.

    Номер изменения — случайная метка в первой строке, а не mtime: у двух
    процессов свои счётчики, а mtime на быстрых дисках успевает повториться
    между двумя записями, и тогда запись принята за собственную.
    """

    def __init__(self, path: str) -> None:
        self._path = path

    def seq(self) -> int:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                token = handle.readline().strip()
        except OSError:
            return 0
        return int(token, 16) if token else 0

    def read(self):
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                handle.readline()
                return handle.read() or None
        except OSError:
            return None

    def write(self, text: str) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(8) + "\n" + text)
        os.replace(tmp, self._path)


def open_store(spec: str):
    """По спецификации --slot: 'win' (по умолчанию), 'memory', 'file:PATH'."""
    if spec in ("win", "windows", "clipboard", ""):
        try:
            return WindowsClipboard()
        except (AttributeError, OSError) as exc:
            raise ClipboardError(
                f"буфер обмена Windows недоступен ({exc}); "
                "для проверки без Windows: --slot file:PATH"
            ) from None
    if spec == "memory":
        return MemoryClipboard()
    if spec.startswith("file:"):
        return FileClipboard(spec[5:])
    raise ClipboardError(f"неизвестный слот {spec!r}: нужно win | memory | file:PATH")


# --- горячая клавиша ---------------------------------------------------------


class Hotkey:
    """Горячая клавиша в отдельном потоке с выборкой сообщений.

    RegisterHotKey(NULL, ...) привязывает комбинацию к потоку, а не к окну, и
    WM_HOTKEY приходит в GetMessage(NULL) этого же потока — окно для этого
    заводить не нужно. Поток демон: при завершении процесса Windows снимает
    регистрацию сама, а остановка идёт через WM_QUIT, чтобы UnregisterHotKey
    отработал и в длинноживущем процессе.
    """

    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    MODS = {"alt": 0x1, "control": 0x2, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
    _ID = 0xB0BA

    class _MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("message", ctypes.c_uint),
            ("wParam", ctypes.c_size_t),
            ("lParam", ctypes.c_ssize_t),
            ("time", ctypes.c_uint),
            ("pt_x", ctypes.c_long),
            ("pt_y", ctypes.c_long),
        ]

    def __init__(self, spec: str, on_press) -> None:
        self.spec = spec
        self._on_press = on_press
        self._mods, self._vk = self.parse(spec)
        self._tid = None
        self._tid_ready = threading.Event()
        self._ready = threading.Event()
        self._error = None
        self._thread = None

    @classmethod
    def parse(cls, spec: str):
        """'ctrl+alt+f9' -> (модификаторы, виртуальный код)."""
        parts = [p.strip().lower() for p in spec.replace("-", "+").split("+") if p.strip()]
        if not parts:
            raise ClipboardError(f"пустая горячая клавиша: {spec!r}")
        mods, key = 0, parts[-1]
        for part in parts[:-1]:
            if part not in cls.MODS:
                raise ClipboardError(
                    f"неизвестный модификатор {part!r} в {spec!r}; "
                    "можно alt, ctrl, shift, win"
                )
            mods |= cls.MODS[part]
        if len(key) == 1:
            vk = ord(key.upper())
        elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
            vk = 0x6F + int(key[1:])
        elif key.startswith("numpad") and key[6:].isdigit() and 0 <= int(key[6:]) <= 9:
            vk = 0x60 + int(key[6:])
        else:
            raise ClipboardError(
                f"не понимаю клавишу {key!r} в {spec!r}; нужна буква, цифра, F1-F24 или numpad0-9"
            )
        return mods, vk

    def start(self) -> None:
        self._user32 = ctypes.windll.user32
        # GetCurrentThreadId — в kernel32; через user32 его нет, а PostThreadMessage
        # обязан попасть именно в тот поток, который зарегистрировал клавишу.
        self._kernel32 = ctypes.windll.kernel32
        self._kernel32.GetCurrentThreadId.restype = ctypes.c_uint
        user32 = self._user32
        user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_uint, ctypes.c_uint]
        user32.GetMessageW.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_uint, ctypes.c_uint]
        user32.PostThreadMessageW.restype = ctypes.c_bool
        user32.PostThreadMessageW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                              ctypes.c_size_t, ctypes.c_ssize_t]
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._thread.start()
        # Регистрация обязана произойти в том же потоке, что крутит GetMessage:
        # при hWnd=NULL клавиша привязывается к вызывающему потоку, и клавиша,
        # зарегистрированная из главного, стучалась бы в очередь главного потока,
        # а не в нашу. Ошибку передаём наружу, а не глотаем в демоне.
        if not self._ready.wait(timeout=5.0):
            raise ClipboardError(f"поток горячей клавиши {self.spec} не запустился")
        if self._error is not None:
            raise ClipboardError(f"комбинация {self.spec} занята другим приложением "
                                 f"(gle={self._error})")

    def _run(self) -> None:
        self._tid = self._kernel32.GetCurrentThreadId()
        self._tid_ready.set()
        # Код ошибки читается немедленно: любое следующее обращение к API его затирает.
        if not self._user32.RegisterHotKey(None, self._ID, self._mods, self._vk):
            self._error = _gle()
            self._ready.set()
            return
        self._ready.set()
        msg = self._MSG()
        while True:
            result = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0 or result < 0:
                # 0 — WM_QUIT, отрицательное — ошибка API: в обоих случаях выход,
                # иначе с -1 цикл превратился бы в занятое ожидание.
                break
            if msg.message == self.WM_HOTKEY:
                self._on_press()
        self._user32.UnregisterHotKey(None, self._ID)

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._error is not None:
            self._thread.join(timeout=0.5)  # поток умер сам, посылать некуда
            self._thread = None
            return
        # PostThreadMessageW(0, ...) разослал бы WM_QUIT всем потокам процесса,
        # а ноль здесь означает и "поток ещё не стартовал". Ждём реальный tid.
        if not self._tid_ready.wait(timeout=2.0):
            self._thread.join(timeout=0.5)
            self._thread = None
            return
        posted = self._user32.PostThreadMessageW(self._tid, self.WM_QUIT, 0, 0)
        # Поток мог завершиться между проверкой и отправкой — тогда ждать нечего.
        self._thread.join(timeout=2.0 if posted else 0.1)
        self._thread = None


# --- транспорт ---------------------------------------------------------------


class ClipboardTransport:
    """Один конец обмена через буфер: кадры <-> текстовый слот.

    sink() существует ради единого интерфейса: у звука перед кадром обязательна
    ведущая тишина (T_LEAD/GAP), у буфера ни пауз, ни тракта нет, поэтому метод
    ничего не делает. Без него tx вызывал бы device.sink() намеренно.
    """

    def __init__(self, store=None, peer_id=None, hotkey=None, auto_pause=True,
                 pause_timeout=60.0, restore=False, on_pause=None) -> None:
        self._store = store if store is not None else open_store("win")
        self.peer_id = peer_id or binascii.hexlify(os.urandom(3)).decode()
        # Номер исходящей ячейки: растёт на каждый send, дедуплицирует свои
        # же записи (GetClipboardSequenceNumber растёт и на SetClipboardData).
        self._out_seq = 0
        # История своих ячеек ( newest-first ), до HISTORY_SLOTS штук.
        # Шаг обмена (CMD → ACK → NOTE) это несколько записей подряд,
        # и при единственной ячейке следующая стёрла бы предыдущую.
        self._my_history = []
        # Принятые чужие ячейки: {id: [(seq, b64), ...]} последних
        # HISTORY_SLOTS. Нужны, чтобы не принять одну и ту же ячейку дважды:
        # текст слота перечитывается при любом изменении номера буфера, а в
        # тексте за раз может стоять несколько чужих кадров.
        self._seen_frames = {}
        self._seen_seq = self._store.seq()
        self._last_out = None
        # Кадры, найденные в слоте, но ещё не отданные вызывающему: очередь,
        # потому что в merge-тексте за одну запись слота могут стоять новые
        # ячейки сразу обеих сторон.
        self._pending = []
        self._lock = threading.Lock()
        self._paused_at = None
        self._pause_timeout = pause_timeout
        self._restore = restore
        # Кнопка паузы меняет поведение молча, а молчание обмена выглядит как
        # обрыв канала. Программа подключает сюда вывод, чтобы человек видел,
        # почему приём встал и каким клавишом его поднять.
        self.on_pause = on_pause
        self.peer = None
        self.paused = False
        # busy — слот занят человеческой копией. Отличается от paused тем, что
        # запрещает только запись: приём буфер не трогает, поэтому читать
        # можно и нужно, иначе любая случайная копия глушила бы канал.
        self.busy = False
        self._busy_at = None
        self.auto_pause = auto_pause
        self._hotkey = None
        if hotkey:
            self._hotkey = Hotkey(hotkey, self.toggle)
            self._hotkey.start()

    # --- состояние обмена --------------------------------------------------

    @property
    def blocked(self) -> bool:
        """Пишем ли мы в слот.

        Здесь только ручная пауза. Чужая копия (busy) запись не блокирует, и
        это не небрежность, а единственная работающая политика. Буфер в
        RDP-сессии почти всегда занят чем-то посторонним, так что запрет
        записи по busy означал бы, что приёмник не может отправить
        подтверждение; а без подтверждения отправчик ретранслирует кадр, и
        обмен не продвигается никогда — тупик, из которого выходила бы только
        ручная пауза. Человек, отдающий буфер программе, решает это явно:
        --hotkey. Автопауза по чужой копии такой цены не стоит.
        """
        return self.paused

    def _notify(self, state: str) -> None:
        if self.on_pause is not None:
            self.on_pause(state)

    def toggle(self) -> bool:
        """Нажатие клавиши: встать/продолжить. Возвращает новое состояние паузы."""
        if self.paused:
            self.resume()
        else:
            self.pause()
        return self.paused

    def pause(self) -> None:
        """Ручная пауза (клавиша): буфер целиком отдаётся человеку.

        Запрещается только запись. Читать буфер по-прежнему можно: приём
        ничего не ломает, а молчание обмена неотличимо от обрыва канала.
        """
        if self.paused:
            return
        self.paused = True
        self._paused_at = time.monotonic()
        self._notify("paused")

    def resume(self) -> None:
        if not self.paused:
            return
        self.paused = False
        self._paused_at = None
        self._reconcile()
        if self._restore and self._last_out is not None and not self.busy:
            # Занять слот можно, только если в нём по-прежнему не человеческая
            # копия: вернуть свой кадр ценой затёртой работы пользователя —
            # ровно то, от чего пауза и существует.
            self._publish(self._last_out)
        self._notify("resumed")

    def _set_busy(self, busy: bool) -> None:
        """Слот занят чужой копией: писать нельзя, читать можно."""
        if self.busy == busy:
            return
        self.busy = busy
        self._busy_at = time.monotonic() if busy else None
        self._notify("busy" if busy else "free")

    def _reconcile(self) -> None:
        """Осмотреть слот на выходе из паузы.

        Молча переставить _seen_seq на текущий было бы потерей: кадр,
        записанный другой стороной, пока мы стояли, оказался бы проглочен, и
        отправчик ушёл бы в повторы ни о чём. Поэтому содержимое читается и
        разбирается здесь же — наш кадр откладывается и будет отдан следующему
        receive(), а человеческая копия просто не разберётся и счётчик
        подтянется, не подняв шума.
        """
        text = self._store.read()
        self._pending.extend(self._accept(text))
        self._seen_seq = self._store.seq()
        # Занятость определяется по фактическому содержимому, а не по флагу:
        # человек мог скопировать своё уже во время паузы, и тогда restore
        # затёр бы его работу — ровно то, ради чего пауза и вводилась.
        if self.auto_pause:
            self._set_busy(self._human_copy(text))

    def _expire(self) -> None:
        """Авто-возобновление: забыть клавишу нажатой легче, чем включить.

        Занятость слота тоже конечна: надёжного сигнала «человек закончил с
        буфером» нет, поэтому молчание дольше таймаута трактуется как
        освобождение — иначе один случайный Ctrl+C выключил бы канал навсегда.
        """
        if not self._pause_timeout:
            return
        now = time.monotonic()
        if self.paused and now - self._paused_at >= self._pause_timeout:
            self.resume()
        if self.busy and now - self._busy_at >= self._pause_timeout:
            self._set_busy(False)

    # --- интерфейс транспорта ---------------------------------------------

    def sink(self, chunk) -> None:
        """Нет-оп: тайминги звукового полудуплекса к буферу не применимы."""

    def send(self, frame_bytes: bytes) -> None:
        self._wait_running(SEND_WAIT_S)
        self._last_out = bytes(frame_bytes)
        self._publish(self._last_out)

    def receive(self, timeout: float):
        """Ждать кадр. Читает и в паузе тоже — см. пояснение к pause()."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            self._expire()

            if self._pending:
                return self._pending.pop(0)

            seq = self._store.seq()
            if seq != self._seen_seq:
                self._seen_seq = seq
                text = self._store.read()
                frames = self._accept(text)
                if frames:
                    self._pending.extend(frames)
                    return self._pending.pop(0)
                if self._human_copy(text) and self.auto_pause:
                    # Кто-то скопировал своё: слот занят, и писать в него
                    # сейчас — значит воевать с человеком за буфер. Приём это
                    # не останавливает: приём ничего не затирает.
                    self._set_busy(True)
                if time.monotonic() >= deadline:
                    return None
                continue

            if time.monotonic() >= deadline:
                return None
            time.sleep(POLL_S)

    def close(self) -> None:
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None

    # --- внутреннее --------------------------------------------------------

    def _publish(self, frame_bytes: bytes) -> None:
        """Записать свою ячейку, сохранив чужие.

        Слот один, а направлений два, поэтому текст слота — это набор
        подписанных ячеек "id:номер:base64". Заменяется только ячейка с нашим
        id, остальные переносятся как есть: иначе ответ B затирал бы команду A
        и обмен был бы невозможен без очереди.

        Номер растёт здесь, а не в send(): публикуется кадр и из send(), и из
        restore на выходе из паузы, а повторный кадр с прежним номером
        собеседник обязан пропустить как уже принятый — значит восстановленный
        кадр обязан получить новый.
        """
        self._out_seq += 1
        # Своя очередь newest-first: шаг обмена (CMD, затем ACK, затем NOTE)
        # это несколько записей подряд, и при единственной ячейке следующая
        # стёрла бы предыдущую, не дав собеседнику её прочитать.
        mine = [(self._out_seq, base64.b64encode(frame_bytes).decode("ascii"))]
        mine.extend(self._my_history[:HISTORY_SLOTS - 1])
        merged = None
        for _ in range(PUBLISH_TRIES):
            # Содержимое перечитывается прямо перед сборкой текста, а не
            # берётся из памяти: окно между «увидели чужую ячейку» и «записали
            # свою» должно быть минимальным, иначе параллельная запись
            # собеседника потерялась бы.
            mark = self._store.seq()
            text_in = self._store.read()
            # Чужие ячейки из перечитанного текста принимаем здесь же, а не
            # ждём следующего receive(): сразу после записи _seen_seq
            # подтянется на текущий номер буфера, и кадр, пришедший во время
            # нашей отправки, не был бы замечен никогда. Для двустороннего
            # обмена это потеря команды.
            self._pending.extend(self._accept(text_in))
            merged = self._merge(mine, self._parse(text_in))
            # Буфер обмена не даёт атомарной операции «прочитать и изменить»,
            # поэтому проверяем номер сама себя перед самой записью: если
            # собеседник успел записать между нашим чтением и сборкой текста,
            # наша запись затёрла бы ячейку, которую мы не видели. Именно так
            # на живом буфере и терялась первая попытка CMD — наш PONG
            # накладывался на уже стёртый им же кадр. Перечитывание дешёвое
            # (GetClipboardSequenceNumber), а окно сжимается до одного вызова.
            if self._store.seq() == mark:
                break
        self._my_history = mine
        self._store.write(merged)
        self._seen_seq = self._store.seq()

    def _merge(self, mine, cells):
        """Текст слота из нашей очереди и чужих ячеек, разобранных из слота."""
        # Версия зашита в PREFIX (см. начало модуля): расхождение форматов сторон
        # обязано дать пустой разбор, а не неверно прочитанный кадр.
        # Чужие стороны переносятся своими списками: подставляя только свою
        # ячейку, мы стёрли бы чужую историю, и собеседник не досчитался бы
        # своих кадров ровно так же, как мы не досчитались бы своих.
        #
        # При этом переносится не всё, что лежит в слоте: чужая история
        # ограничена HISTORY_SLOTS так же, как своя. Без этого текста слота
        # рос бы без потолка — например после нескольких перезапусков
        # собеседника с новыми id, — а слот обязан оставаться маленьким,
        # потому что перед каждой записью его перечитывает другая сторона.
        # Внутри одной стороны номера монотонны, поэтому «самые свежие» —
        # это просто самые большие номера.
        groups = [(self.peer_id, mine)] + [
            (who, sorted(chain, reverse=True)[:HISTORY_SLOTS])
            for who, chain in sorted(cells.items())
            if who != self.peer_id
        ]
        return PREFIX + "".join(
            SEPARATOR + "%s%s%s%s%s" % (who, FIELD, num, FIELD, b64)
            for who, chain in groups
            for num, b64 in chain
        )

    def _parse(self, text):
        """Текст слота -> {id: [(номер, base64), ...]}. Неразбираемое -> пусто.

        Ячейки плоские — "id:номер:base64" через '|', поэтому одна сторона
        занимает несколько ячеек подряд. id не должен содержать ':' и '|',
        иначе разбор отдельной ячейки станет неоднозначным.
        """
        if not text:
            return {}
        parts = text.split(SEPARATOR)
        if len(parts) < 2 or parts[0] != PREFIX:
            return {}
        cells = {}
        for part in parts[1:]:
            fields = part.split(FIELD)
            if len(fields) != 3:
                continue
            who, num, b64 = fields
            if not who or not num.isdigit():
                continue
            cells.setdefault(who, []).append((int(num), b64))
        return cells

    def _accept(self, text):
        """Текст слота -> список новых чужих кадров (байты), старые первыми.

        Новый кадр — это пара (номер, содержимое), которой мы ещё не принимали.
        Ровно пара, а не номер: повтор ячейки даёт тот же base64, тогда как
        новый кадр после перезапуска собеседника приходит с новым содержимым и
        совпавшим номером (его счётчик снова пошёл с единицы). Решать по
        одному номеру значило бы оглохнуть навсегда после рестарта второй
        стороны; решать по одному содержимому значило бы потерять вторую
        одинаковую команду подряд.

        Порядок — по возрастанию номера, а не в порядке ячеек слота: своя
        история пишется от новых к старым, и ACK был бы отдан раньше CMD.
        """
        found = []
        for who, chain in sorted(self._parse(text).items()):
            if who == self.peer_id:
                continue
            seen = self._seen_frames.setdefault(who, [])
            for num, b64 in sorted(chain):
                if (num, b64) in seen:
                    continue
                try:
                    data = base64.b64decode(b64, validate=True)
                except (binascii.Error, ValueError):
                    continue
                self._seen_frames.setdefault(who, []).append((num, b64))
                self.peer = who
                found.append(data)
        return found

    def reset_seen(self) -> None:
        """Забыть принятые кадры: начало нового сеанса (шаг восстановления)."""
        self._seen_frames.clear()
        self._my_history = []

    def _human_copy(self, text) -> bool:
        """Похож ли слот на копию человека, а не на наш испорченный кадр.

        Различие принципиально: текст с нашим префиксом — это наш кадр,
        доехавший побитым, и занимать из-за него слот нельзя, обмен сам себя
        выключит на первой же помехе. Занятость — ответ только на чужую копию,
        то есть на текст без префикса.
        """
        return bool(text) and not text.startswith(PREFIX + SEPARATOR)

    def _wait_running(self, wait_s: float) -> None:
        """Ждать, пока слот освободится для записи.

        Ждём именно blocked, а не paused: отправка затирает буфер, поэтому и
        ручная пауза, и человеческая копия её запрещают. Приём под паузу не
        подпадает — он ничего не затирает.
        """
        deadline = time.monotonic() + wait_s
        while self.blocked:
            if time.monotonic() >= deadline:
                how = f"нажмите {self._hotkey.spec}" if self._hotkey else "снимите паузу"
                raise ClipboardError(
                    f"буфер занят уже {wait_s:.0f} с ({'пауза' if self.paused else 'чужая копия'}) — "
                    f"{how}, чтобы отправить кадр"
                )
            time.sleep(POLL_S)

    @property
    def describe(self) -> str:
        hotkey = self._hotkey.spec if self._hotkey else "нет"
        state = "пауза" if self.paused else ("занят чужой копией" if self.busy else "работает")
        return (f"слот={type(self._store).__name__} я={self.peer_id} "
                f"клавиша={hotkey} состояние={state}")
