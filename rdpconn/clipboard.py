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

from . import config

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

# Сколько последних ячеек одной стороны остаётся в тексте слота.
# Число живёт в config: это потолок протокола, и он должен вмещать
# весь ответ --exec, а не только ACK и пару NOTE.
HISTORY_SLOTS = config.HISTORY_SLOTS

# По умолчанию: Ctrl+Alt+F9. Дальше от стандартных связок Word/Excel/терминалов.
DEFAULT_HOTKEY = "ctrl+alt+f9"

# Опрос буфера. 50 мс — компромисс: ниже порога ощущаемой задержки обмена
# (кадр всё равно собирается дольше), но неBusyWait, греющий CPU.
POLL_S = 0.05
# Редирект сначала двигает номер буфера, а текст подставляет следом.
# Пока номер тот же, буфер не открываем. После нового номера, если строка
# ещё старая, сверяем её раз в секунду всё время ожидания и номер не
# закрываем.
STALE_RECHECK_S = 1.0
# Один повтор той же полезной нагрузки новой ячейкой, если за это время
# ответ не пришёл. Сам по себе повтор снимок не лечит: пока идёт серия
# записей, ответ в слот не садится. Живой путь его не заказывает.
# Стартовое значение. После замера rdpconn.tune ставит предел × PACE_MARGIN.
NUDGE_AFTER_S = 1.0
# Столько байт одной записи редирект увозит: первая часто остаётся на хосте,
# повтор тех же байт новой ячейкой через секунду доходит. Крупнее этого
# первая запись на хосте и остаётся, и тот же повтор её не вытаскивает.
SNAPSHOT_MAX = 16384

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


def snapshot_pieces(payload: bytes) -> list[bytes]:
    """Нарезать нагрузку на снимки, которые редирект увозит по одному.

    Следующий кусок пишут после ответа на текущий. Склеивать их в одну
    запись нельзя: слот снова становится тем размером, который с хоста
    не уезжает.
    """
    if len(payload) <= SNAPSHOT_MAX:
        return [payload]
    return [
        payload[i:i + SNAPSHOT_MAX]
        for i in range(0, len(payload), SNAPSHOT_MAX)
    ]


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
            # Не None: пустой слот тоже None, и вызывающий не отличил бы
            # «буфер занят» от «текста нет». Занятый буфер надо перечитать.
            raise ClipboardError(
                "OpenClipboard не открылся: буфер занят другим приложением")
        try:
            handle = self._user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                return None
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                raise ClipboardError("GlobalLock не удалось")
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
    # Без этого флага удержание клавиши шлёт WM_HOTKEY повторно, и второе
    # сообщение снимает паузу, которую первое только что поставило.
    MOD_NOREPEAT = 0x4000
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
        # Сначала с MOD_NOREPEAT. Если система его не знает — без флага.
        # Несколько попыток: после убийства прошлого rx комбинация ещё долю
        # секунды числится занятой, и новый приёмник поднимался бы без клавиши.
        registered = False
        last_error = 0
        for _ in range(8):
            for mods in (self._mods | self.MOD_NOREPEAT, self._mods):
                if self._user32.RegisterHotKey(None, self._ID, mods, self._vk):
                    registered = True
                    break
                last_error = _gle()
            if registered:
                break
            time.sleep(0.2)
        if not registered:
            self._error = last_error
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
                try:
                    self._on_press()
                except Exception as exc:
                    # Исключение здесь убивает поток. Следующие нажатия тогда
                    # пропадают молча: пауза «не срабатывает», хотя процесс жив.
                    try:
                        from . import app
                        app.warn(f"!! горячая клавиша: {exc}")
                    except Exception:
                        pass
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
        self._seen_seq = 0
        self._last_text = None
        # Номер, который уже виден, но текст по нему ещё не прочитан.
        self._pending_seq = None
        self._read_at = 0.0
        # Ячейки, которые уже лежали в слоте, — не наш пропущенный кадр:
        # процесс только что стартовал. Иначе старый ACK с seq=0 принимается
        # за ответ на команду, которую этот процесс ещё только пошлёт.
        self._remember_slot()
        self._last_out = None
        # Один отложенный повтор последней записи новыми номерами ячеек.
        self._nudge_at = None
        self._nudge_mine = None
        self._nudge_once = False
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
        try:
            self._reconcile()
            if self._restore and self._last_out is not None and not self.busy:
                # Занять слот можно, только если в нём по-прежнему не человеческая
                # копия: вернуть свой кадр ценой затёртой работы пользователя —
                # ровно то, от чего пауза и существует.
                self._publish(self._last_out)
        except ClipboardError as exc:
            # Пауза уже снята. Ошибка чтения слота не должна убить поток
            # клавиши: иначе следующее нажатие некому принять.
            from . import app
            app.warn(f"!! слот не перечитан при продолжении: {exc}")
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
        self._last_text = text
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
        nudge = self._nudge_once
        self._nudge_once = False
        self._publish_many([self._last_out], keep_history=True, schedule_nudge=nudge)

    def send_many(self, frames) -> None:
        """Весь ответ одной записью буфера.

        ACK и каждый NOTE отдельным SetClipboardData оставляют редиректу
        промежуточный снимок: он уезжает на ту сторону и финальной записью
        уже не заменяется, хвост вывода теряется. Пачка получает номера
        сразу и ложится в слот одним текстом.
        """
        blob = [bytes(raw) for raw in frames]
        if not blob:
            return
        self._wait_running(SEND_WAIT_S)
        self._last_out = blob[-1]
        # Старая история в эту запись не входит: иначе следующая команда
        # читает NOTE предыдущей и принимает его за свой ответ.
        nudge = self._nudge_once
        self._nudge_once = False
        self._publish_many(blob, keep_history=False, schedule_nudge=nudge)

    def peek_pending(self):
        """Кадр, уже разобранный из слота, но ещё не отданный наружу."""
        return self._pending[0] if self._pending else None

    def pop_pending(self):
        if self._pending:
            return self._pending.pop(0)
        return None

    def receive(self, timeout: float):
        """Ждать кадр. Читает и в паузе тоже — см. пояснение к pause()."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            self._expire()

            if self._pending:
                return self._pending.pop(0)

            self._maybe_nudge()

            now = time.monotonic()
            seq = self._store.seq()
            # Пока номер молчит, буфер не открываем. Редирект как раз в это
            # время записывает ответ, и OpenClipboard ему мешает.
            if seq != self._seen_seq:
                if self._pending_seq != seq:
                    self._pending_seq = seq
                    # Первое чтение сразу: на слоте в памяти текст уже новый,
                    # и короткие таймауты тестов на это рассчитаны.
                    self._read_at = now
                if now < self._read_at:
                    if now >= deadline:
                        return None
                    pause = min(self._read_at - now, deadline - now)
                    time.sleep(pause)
                    continue
                try:
                    text = self._store.read()
                except ClipboardError:
                    # Номер не двигаем. Иначе кадр останется в слоте, а
                    # следующий опрос решит, что ничего не менялось.
                    if now >= deadline:
                        return None
                    time.sleep(POLL_S)
                    continue
                # Пока строка прежняя, номер открыт. Следующий receive()
                # продолжит ждать: ожидание длиннее одного вызова.
                if text == self._last_text:
                    self._read_at = now + STALE_RECHECK_S
                else:
                    self._seen_seq = seq
                    self._pending_seq = None
                    got = self._ingest(text)
                    if got is not None:
                        return got
                if now >= deadline:
                    return None
                pause = min(POLL_S, deadline - now)
                if self._read_at > now:
                    pause = min(pause, self._read_at - now)
                time.sleep(pause)
                continue

            if now >= deadline:
                return None
            pause = min(POLL_S, deadline - now)
            if self._nudge_at is not None:
                pause = min(pause, max(0.0, self._nudge_at - now))
            time.sleep(pause)

    def _maybe_nudge(self) -> None:
        """Один раз переиздать последнюю запись новыми номерами ячеек.

        Тот же base64 под новым номером собеседник ещё не принимал, поэтому
        он снова увидит кадр и ответит из кэша, не исполняя команду второй
        раз. После этого буфер больше не трогаем: серия записей стирает ответ.

        Чужой кадр уже в очереди — писать нельзя, он стёрся бы. Таймер
        сдвигается на секунду, а не сбрасывается: иначе единственный повтор
        пропал бы.
        """
        if self._nudge_at is None or time.monotonic() < self._nudge_at:
            return
        if self._pending:
            self._nudge_at = time.monotonic() + NUDGE_AFTER_S
            return
        mine = self._nudge_mine
        self._nudge_at = None
        self._nudge_mine = None
        if mine is None or self._my_history is not mine or self.paused:
            return
        frames = [base64.b64decode(b64) for _, b64 in reversed(mine)]
        if not frames:
            return
        self._publish_many(frames, keep_history=False, schedule_nudge=False)

    def close(self) -> None:
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None

    # --- внутреннее --------------------------------------------------------

    def _publish(self, frame_bytes: bytes) -> None:
        """Записать одну свою ячейку, не стирая непрочитанные предыдущие.

        На живом буфере запись после чужого снимка часто не уезжает.
        Следующая, уже с новым номером ячейки, уезжает. Один такой повтор
        заказывается здесь и снимается, если ответ успел прийти раньше.
        Крупнее SNAPSHOT_MAX одной записью не пишется: следующую часть
        вызывает уже тот, кто дождался ответа на эту.
        """
        if len(frame_bytes) > SNAPSHOT_MAX:
            raise ValueError(
                f"снимок {len(frame_bytes)} байт больше {SNAPSHOT_MAX}: "
                "следующую часть пишут после ответа на эту"
            )
        self._publish_many([frame_bytes], keep_history=True, schedule_nudge=True)

    def _schedule_nudge(self) -> None:
        if not isinstance(self._store, WindowsClipboard) or self.paused:
            return
        self._nudge_mine = self._my_history
        self._nudge_at = time.monotonic() + NUDGE_AFTER_S

    def _publish_many(self, frames, keep_history: bool = True,
                      schedule_nudge: bool = True) -> None:
        """Записать свои ячейки одной операцией, сохранив чужие.

        Слот один, а направлений два, поэтому текст слота — это набор
        подписанных ячеек "id:номер:base64". Заменяется только цепочка с
        нашим id, остальные переносятся как есть: иначе ответ B затирал бы
        команду A и обмен был бы невозможен без очереди.

        Номер растёт здесь, а не в send(): публикуется кадр и из send(), и из
        restore на выходе из паузы, а повторный кадр с прежним номером
        собеседник обязан пропустить как уже принятый — значит восстановленный
        кадр обязан получить новый. Повторная запись той же пачки номера не
        меняет: новый номер был бы для собеседника вторым ответом.
        """
        fresh = []
        for raw in frames:
            self._out_seq += 1
            fresh.append((self._out_seq, base64.b64encode(raw).decode("ascii")))
        # Своя очередь newest-first. В слот входит вся пачка, а не только
        # последняя ячейка: иначе редирект увозил бы хвост без ACK.
        mine = list(reversed(fresh))
        if len(mine) > HISTORY_SLOTS:
            mine = mine[:HISTORY_SLOTS]
        elif keep_history:
            mine.extend(self._my_history[:HISTORY_SLOTS - len(mine)])
        self._my_history = mine
        head = mine[0]
        merged = self._merge(mine, {})
        landed = False
        for _ in range(PUBLISH_TRIES):
            # Содержимое перечитывается прямо перед сборкой текста, а не
            # берётся из памяти: окно между «увидели чужую ячейку» и «записали
            # свою» должно быть минимальным, иначе параллельная запись
            # собеседника потерялась бы.
            try:
                mark = self._store.seq()
                text_in = self._store.read()
            except ClipboardError:
                continue
            # Чужие ячейки из перечитанного текста принимаем здесь же, а не
            # ждём следующего receive(): сразу после записи _seen_seq
            # подтянется на текущий номер буфера, и кадр, пришедший во время
            # нашей отправки, не был бы замечен никогда. Для двустороннего
            # обмена это потеря команды.
            self._pending.extend(self._accept(text_in))
            foreign = self._parse(text_in)
            # На редиректе уже принятая чужая ячейка в следующей записи
            # превращает снимок в эхо, и новый кадр до собеседника не доезжает.
            # В памяти и в файле слот один на обоих: чужую ячейку стирать
            # нельзя, её ещё не прочитали.
            if isinstance(self._store, WindowsClipboard):
                foreign = self._unseen_foreign(text_in)
            merged = self._merge(mine, foreign)
            # Буфер обмена не даёт атомарной операции «прочитать и изменить»,
            # поэтому проверяем номер сама себя перед самой записью: если
            # собеседник успел записать между нашим чтением и сборкой текста,
            # наша запись затёрла бы ячейку, которую мы не видели. Именно так
            # на живом буфере и терялась первая попытка CMD — наш PONG
            # накладывался на уже стёртый им же кадр. Перечитывание дешёвое
            # (GetClipboardSequenceNumber), а окно сжимается до одного вызова.
            if self._store.seq() != mark:
                continue
            self._store.write(merged)
            self._note_local_write(merged)
            landed = True
            # Редирект иногда оставляет в слоте предыдущий снимок. Пока своей
            # новой ячейки не видно, пишем тот же номер ещё раз: новый номер
            # был бы для собеседника вторым кадром.
            if self._cell_visible(head):
                break
        else:
            if not landed:
                self._store.write(merged)
                self._note_local_write(merged)
        if schedule_nudge:
            self._schedule_nudge()

    def _note_local_write(self, merged: str) -> None:
        """Запомнить свою запись. Ответ ждём по новому номеру, буфер не трогаем."""
        self._seen_seq = self._store.seq()
        self._last_text = merged
        self._pending_seq = None

    def _ingest(self, text):
        """Разобрать текст, если он отличается от уже увиденного.

        Возвращает один кадр или None. None значит «кадра нет», а не ошибку:
        тот же текст и человеческая копия — нормальный результат опроса.
        """
        if text == self._last_text:
            return None
        self._last_text = text
        frames = self._accept(text)
        if frames:
            # Ответ уже у нас. Повтор своей записи стёр бы его.
            self._nudge_at = None
            self._pending.extend(frames)
            return self._pending.pop(0)
        if self.auto_pause:
            if self._human_copy(text):
                # Кто-то скопировал своё: слот занят, и писать в него
                # сейчас — значит воевать с человеком за буфер. Приём это
                # не останавливает: приём ничего не затирает.
                self._set_busy(True)
            elif text and text.startswith(PREFIX + SEPARATOR):
                self._set_busy(False)
        return None

    def _unseen_foreign(self, text):
        """Чужие ячейки, которых эта сторона ещё не принимала."""
        kept = {}
        for who, chain in self._parse(text or "").items():
            if who == self.peer_id:
                continue
            seen = self._seen_frames.get(who, [])
            fresh = [pair for pair in chain if pair not in seen]
            if fresh:
                kept[who] = fresh
        return kept

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

    def _remember_slot(self) -> None:
        """Пометить текущие чужие ячейки уже виденными, не отдавая их наружу."""
        try:
            text = self._store.read()
            seq = self._store.seq()
        except ClipboardError:
            return
        self._seen_seq = seq
        self._last_text = text
        if not text:
            return
        for who, chain in self._parse(text).items():
            if who == self.peer_id:
                continue
            seen = self._seen_frames.setdefault(who, [])
            for num, b64 in chain:
                pair = (num, b64)
                if pair not in seen:
                    seen.append(pair)

    def _cell_visible(self, head) -> bool:
        """Видна ли в слоте только что записанная своя ячейка."""
        try:
            text = self._store.read()
        except ClipboardError:
            return False
        num, b64 = head
        return any(
            n == num and b == b64
            for n, b in self._parse(text).get(self.peer_id, ())
        )

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
