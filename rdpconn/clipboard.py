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

Хранилище отслоено (WindowsClipboard / MemoryClipboard / FileClipboard /
DelayedClipboard), чтобы вся логика — маркер, фильтрация своих кадров,
занятость, пауза, restore — проверялась без Windows-API и без второй машины.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import os
import random
import secrets
import threading
import time

from . import config, statcell

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
# Номер буфера приходит раньше текста. Пока номер молчит, буфер не открываем.
# После нового номера строка читается сразу, затем несколько раз часто:
# текст часто догоняет номер за десятые доли секунды. Если и тогда строка
# старая, сверка возвращается к разу в секунду, чтобы не держать OpenClipboard.
STALE_RECHECK_S = 1.0
STALE_FAST_S = 0.05
STALE_FAST_TRIES = 8
# Повтор той же полезной нагрузки новой ячейкой, если за это время
# ответ не пришёл. Сам по себе повтор снимок не лечит: пока идёт серия
# записей, ответ в слот не садится. Живой путь его не заказывает.
# На стенде повтор ещё доходил при 0.7 с и начинал стирать ответ при 0.6 с.
# В работе предел × PACE_MARGIN: 0.7 × 1.33 = 0.94 с.
NUDGE_AFTER_S = 0.94
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
    # SetClipboardData ловит gle=1418 (ERROR_CLIPBOARD_NOT_OPEN), даже когда
    # OpenClipboard только что отдал успех: между этими двумя вызовами нет
    # атомарности, и чужой процесс может перехватить буфер в этой щели —
    # воспроизведено локально гонкой двух процессов (~0.22% попыток при
    # тугом цикле, без каскада), память gle_1418_clipboard_race.md. Это не
    # повторная проверка OpenClipboard (_open уже даёт 12 попыток входа),
    # а отдельный, более короткий retry именно вокруг самой записи.
    _SET_RETRIES = 5
    _SET_WAIT_S = 0.02

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
        # Без явного argtypes ctypes угадывает int по умолчанию и 64-битный
        # хэндл не проходит (OverflowError) — путь, который реально этот
        # вызов исполняет (отказ SetClipboardData после всех retry), раньше
        # не существовал вовсе, поэтому пропуск здесь был незаметен.
        k.GlobalFree.argtypes = [ctypes.c_void_p]

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

    def try_read(self):
        """Одно открытие, без повторов.

        Наблюдатель так не держит слот, пока его пишет обмен: занятый буфер
        просто ошибка, и следующая проверка номера попробует ещё раз.
        """
        if not self._user32.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
            return None
        if not self._user32.OpenClipboard(None):
            raise ClipboardError("буфер занят другим приложением")
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

        last_gle = 0
        for attempt in range(self._SET_RETRIES):
            if not self._open():
                self._kernel32.GlobalFree(handle)
                raise ClipboardError("OpenClipboard не открылся: буфер занят другим приложением")
            try:
                self._user32.EmptyClipboard()
                # После успешной SetClipboardData владеет блоком буфер обмена:
                # GlobalFree здесь означал бы двойное освобождение.
                if self._user32.SetClipboardData(self.CF_UNICODETEXT, handle):
                    return
                last_gle = _gle()
            finally:
                self._user32.CloseClipboard()
            if attempt + 1 < self._SET_RETRIES:
                time.sleep(self._SET_WAIT_S)
        # Сюда доходим только если буфер так и не принял блок ни разу:
        # владение им клипборд не получил, освобождать блок — наша забота.
        self._kernel32.GlobalFree(handle)
        raise ClipboardError(f"SetClipboardData не удалось (gle={last_gle})")

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

    def try_read(self):
        return self.read()

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

    def try_read(self):
        return self.read()

    def write(self, text: str) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(8) + "\n" + text)
        os.replace(tmp, self._path)


class DelayedClipboard:
    """Обёртка над другим слотом с искусственной задержкой публикации.

    Моделирует не время вызова, а лаг самого редиректа RDP (AGENTS.md 3,6:
    живой буфер запаздывает на 10-15 с) — запись становится видна читателю
    не раньше чем через delay ± jitter после write(), а не сразу. Задержка
    симметрична и видна в том числе самому писавшему: настоящий редирект для
    удалённой стороны всегда не лучше этого, поэтому протокол, выдержавший
    такую (более жёсткую) задержку, с запасом выдержит и живой RDP — но числа,
    завязанные на физику самого редиректа (потолок ячеек шлюза, docs/protocol.md
    13), эта симуляция не заменяет, только протокольную логику (heartbeat,
    повторы, сборка сегментов).

    Применение отложенной записи не требует фонового потока: он происходит
    при следующем read()/try_read()/seq() этого же объекта, как только истёк
    срок, — вызывающий код и так опрашивает слот в цикле, лишний поток не
    нужен. Если запись перезаписывается до того как предыдущая успела
    примениться — ждёт только последняя, как и в настоящем буфере: он хранит
    одно значение, а не очередь.
    """

    def __init__(self, store, delay: float, jitter: float = 0.0) -> None:
        self._store = store
        self._delay = max(0.0, delay)
        self._jitter = max(0.0, jitter)
        self._lock = threading.Lock()
        self._pending = None  # (срок, текст) — ещё не применённая запись

    def _settle(self) -> None:
        with self._lock:
            if self._pending is None:
                return
            deadline, text = self._pending
            if time.monotonic() < deadline:
                return
            self._pending = None
        self._store.write(text)

    def seq(self) -> int:
        self._settle()
        return self._store.seq()

    def read(self):
        self._settle()
        return self._store.read()

    def try_read(self):
        self._settle()
        return self._store.try_read()

    def write(self, text: str) -> None:
        delay = self._delay + (random.uniform(0.0, self._jitter) if self._jitter else 0.0)
        with self._lock:
            self._pending = (time.monotonic() + delay, text)


def open_store(spec: str):
    """По спецификации --slot: 'win' (по умолчанию), 'memory', 'file:PATH',
    'sim:DELAY[,JITTER][:PATH]' (без PATH — поверх memory, с PATH — поверх file)."""
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
    if spec.startswith("sim:"):
        timing, _, path = spec[4:].partition(":")
        delay_s, _, jitter_s = timing.partition(",")
        try:
            delay = float(delay_s)
            jitter = float(jitter_s) if jitter_s else 0.0
        except ValueError:
            raise ClipboardError(
                f"sim: нужна задержка числом (sim:DELAY[,JITTER][:PATH]), не {timing!r}"
            ) from None
        base = FileClipboard(path) if path else MemoryClipboard()
        return DelayedClipboard(base, delay, jitter)
    raise ClipboardError(
        f"неизвестный слот {spec!r}: нужно win | memory | file:PATH | sim:DELAY[,JITTER][:PATH]"
    )


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


class PasteWatch:
    """Замечает вставку, не отбирая её у программы.

    Ctrl+V и Shift+Insert остаются у того окна, где их нажали: хук
    низкого уровня только сдвигает срок, когда шлюз снова займёт слот.
    Канал команд этот наблюдатель не включает.
    """

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_SYSKEYDOWN = 0x0104
    WM_QUIT = 0x0012
    VK_SHIFT = 0x10
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    VK_V = 0x56
    VK_INSERT = 0x2D
    LLKHF_UP = 0x80

    class _KEY(ctypes.Structure):
        _fields_ = [
            ("vkCode", ctypes.c_uint),
            ("scanCode", ctypes.c_uint),
            ("flags", ctypes.c_uint),
            ("time", ctypes.c_uint),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    def __init__(self, on_paste) -> None:
        self._on_paste = on_paste
        self._thread = None
        self._hook = None
        self._tid = None
        self._tid_ready = threading.Event()
        self._ready = threading.Event()
        self._error = None
        self._callback = None
        self._down = set()

    def start(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._user32 = user32
        self._kernel32 = kernel32
        kernel32.GetCurrentThreadId.restype = ctypes.c_uint
        user32.GetMessageW.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_uint, ctypes.c_uint]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_size_t, ctypes.c_ssize_t]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_uint]
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.PostThreadMessageW.restype = ctypes.c_bool
        user32.PostThreadMessageW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                              ctypes.c_size_t, ctypes.c_ssize_t]
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                      ctypes.c_size_t, ctypes.c_ssize_t)
        self._callback = HOOKPROC(self._proc)
        self._thread = threading.Thread(target=self._run, name="paste-watch", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise ClipboardError("наблюдатель вставки не запустился")
        if self._error is not None:
            raise ClipboardError(f"наблюдатель вставки не встал (gle={self._error})")

    def _down_mod(self, vk: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    def _proc(self, nCode, wParam, lParam):
        if nCode >= 0 and wParam in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN) and lParam:
            key = ctypes.cast(lParam, ctypes.POINTER(self._KEY)).contents
            up = bool(key.flags & self.LLKHF_UP)
            if up:
                self._down.discard(key.vkCode)
            elif key.vkCode not in self._down:
                self._down.add(key.vkCode)
                ctrl = self._down_mod(self.VK_CONTROL)
                shift = self._down_mod(self.VK_SHIFT)
                alt = self._down_mod(self.VK_MENU)
                paste = (
                    (key.vkCode == self.VK_V and ctrl and not alt)
                    or (key.vkCode == self.VK_INSERT and shift and not ctrl and not alt)
                )
                if paste:
                    try:
                        self._on_paste()
                    except Exception:
                        pass
        return self._user32.CallNextHookEx(self._hook, nCode, wParam, lParam)

    def _run(self) -> None:
        self._tid = self._kernel32.GetCurrentThreadId()
        self._tid_ready.set()
        self._hook = self._user32.SetWindowsHookExW(
            self.WH_KEYBOARD_LL, self._callback, None, 0)
        if not self._hook:
            self._error = _gle()
            self._ready.set()
            return
        self._ready.set()
        msg = Hotkey._MSG()
        while True:
            result = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0 or result < 0:
                break
        self._user32.UnhookWindowsHookEx(self._hook)
        self._hook = None

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._error is not None:
            self._thread.join(timeout=0.5)
            self._thread = None
            return
        if not self._tid_ready.wait(timeout=2.0):
            self._thread.join(timeout=0.5)
            self._thread = None
            return
        posted = self._user32.PostThreadMessageW(self._tid, self.WM_QUIT, 0, 0)
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
        self._stale_tries = 0
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
        # Окно, когда receive() не трогает буфер вовсе — ни seq(), ни read().
        # Сильнее paused: та пауза блокирует только запись (приём по
        # прежнему читает, см. докстринг pause()); для F1 (fileshake.py)
        # этого не хватило — свой же классический GetClipboardData во
        # время wait_result() способен сорвать ещё не отданный FileContents
        # того же буфера (HRESULT 0x80040064, см. память проекта). См.
        # suspend_reads().
        self._reads_suspended_until = 0.0
        self._pause_timeout = pause_timeout
        self._restore = restore
        # Кнопка паузы меняет поведение молча, а молчание обмена выглядит как
        # обрыв канала. Программа подключает сюда вывод, чтобы человек видел,
        # почему приём встал и каким клавишом его поднять.
        self.on_pause = on_pause
        # Шлюз подставляет сюда отправку одного служебного кадра: пауза на
        # этой стороне сама по себе не мешает собеседнику писать снимок
        # через секунду и снова занимать слот.
        self.before_pause = None
        self.after_resume = None
        # Поток, который прямо сейчас объявляет паузу, ещё имеет право
        # записать её сигнал. Чужой поток в это окно уже писать не должен.
        self._yield_thread = None
        # Служебный кадр паузы держим в каждом следующем снимке, пока
        # собеседник не запишет свой: иначе наш же ответ затирает сигнал,
        # и вторая сторона так и остаётся на паузе.
        self._pin_signal = None
        self._pin_ready = False
        self._pin_seen = {}
        # Шлюз отдаёт слот человеку, не посылая служебный кадр: кадр стёр бы
        # только что скопированный текст. tx/rx это не включает — иначе
        # чужая копия навсегда лишила бы приёмник возможности подтвердить кадр.
        self.yield_human = False
        self.human_quiet_s = 2.0
        self.human_cap_s = 15.0
        self.yielding = False
        self._human_text = None
        self._human_until = 0.0
        self._human_pasted = False
        self._human_released_text = None
        self._paste_watch = None
        self.peer = None
        self.paused = False
        # busy — слот занят человеческой копией. Отличается от paused тем, что
        # запрещает только запись: приём буфер не трогает, поэтому читать
        # можно и нужно, иначе любая случайная копия глушила бы канал.
        self.busy = False
        self._busy_at = None
        # Гуест подставляет сюда сборку одной служебной ячейки. Хост — сводку.
        # Пусто — снимок как раньше, без лишней ячейки.
        self.stats_frame = None
        self.on_stats = None
        self.picture = None
        self.board = None
        self._meter_out = 0
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

    def _write_held(self) -> bool:
        """Нельзя ли сейчас класть кадр в слот.

        Поток, который объявляет паузу, дописывает её сигнал. Любой другой
        поток в этом окне уже опоздал: его запись легла бы поверх сигнала
        и собеседник паузу не увидел бы.
        """
        owner = self._yield_thread
        me = threading.get_ident()
        if owner not in (None, me):
            return True
        return self.paused and owner != me

    def pause(self, *, announce: bool = True) -> None:
        """Ручная пауза (клавиша): буфер целиком отдаётся человеку.

        Запрещается только запись. Читать буфер по-прежнему можно: приём
        ничего не ломает, а молчание обмена неотличимо от обрыва канала.
        announce=False — пауза пришла от собеседника, второй сигнал не шлём,
        иначе стороны перекликались бы без конца.
        """
        me = threading.get_ident()
        if self.paused or self._yield_thread not in (None, me):
            return
        self._nudge_at = None
        self._nudge_mine = None
        self._yield_thread = me
        try:
            if announce and self.before_pause is not None:
                try:
                    self.before_pause()
                except Exception as exc:
                    from . import app
                    app.warn(f"!! сигнал паузы: {exc}")
        finally:
            self._nudge_at = None
            self._nudge_mine = None
            self.paused = True
            self._paused_at = time.monotonic()
            self._yield_thread = None
        self._notify("paused")

    def resume(self, *, announce: bool = True) -> None:
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
        if announce and self.after_resume is not None:
            try:
                self.after_resume()
            except Exception as exc:
                from . import app
                app.warn(f"!! сигнал продолжения: {exc}")

    def suspend_reads(self, duration: float) -> None:
        """Ближайшие duration секунд receive() не трогает буфер вовсе.

        Не то же самое, что pause(): та оставляет чтение разрешённым
        ("приём ничего не ломает"), а для F1 (fileshake.py) это оказалось
        не так — собственный классический GetClipboardData во время
        ожидания FILE_DONE способен сорвать ещё не отданный FileContents
        того же буфера (HRESULT 0x80040064, живой фикс 27.11.2026: пауза
        только записи не спасала, вызывающая сторона всё равно опрашивала
        буфер в ожидании ответа). Новый вызов продлевает срок, но не
        укорачивает — два источника (например, send_files() и его
        собственный повтор) не должны отменять друг друга по ошибке.
        """
        until = time.monotonic() + max(0.0, duration)
        if until > self._reads_suspended_until:
            self._reads_suspended_until = until

    def note_paste(self) -> None:
        """Вставка случилась: канал ещё немного молчит, затем возвращается.

        Само сочетание клавиш буфер не трогает. Новая копия заводит срок
        заново и длиннее: человек мог ещё не дойти до окна, куда вставляет.
        """
        if not self.yield_human or self.paused:
            return
        now = time.monotonic()
        if not self.yielding:
            self._human_pasted = True
            self._human_until = now + self.human_quiet_s
            self.yielding = True
            self._nudge_at = None
            self._nudge_mine = None
            self._notify("yield")
            return
        self._human_pasted = True
        self._human_until = now + self.human_quiet_s

    def consider_slot(self) -> None:
        """Снять молчание, когда срок после вставки (или без неё) вышел."""
        if not self.yield_human or self.paused or not self.yielding:
            return
        if time.monotonic() >= self._human_until:
            self._release_human()

    def _hold_human(self, text: str) -> None:
        if text == self._human_released_text and not self.yielding:
            return
        now = time.monotonic()
        first = not self.yielding
        if text != self._human_text:
            previous = self._human_text
            self._human_text = text
            if previous is not None:
                self._human_pasted = False
                self._human_until = now + self.human_cap_s
            elif not self._human_pasted:
                self._human_until = now + self.human_cap_s
        elif first:
            self._human_until = now + self.human_cap_s
        self.yielding = True
        self._nudge_at = None
        self._nudge_mine = None
        if first:
            self._notify("yield")

    def _release_human(self) -> None:
        if not self.yielding:
            return
        self._human_released_text = self._human_text
        self.yielding = False
        self._human_text = None
        self._human_pasted = False
        self._human_until = 0.0
        self._notify("yield_done")

    def _refuse_human(self, text) -> bool:
        """Не писать поверх текста человека. False — эту запись делать можно."""
        if not self.yield_human or not text or not self._human_copy(text):
            return False
        if text == self._human_released_text:
            return False
        self._hold_human(text)
        return True

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

    def send_many(self, frames, limit: int | None = None) -> None:
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
        self._publish_many(blob, keep_history=False, schedule_nudge=nudge, limit=limit)

    def peek_pending(self):
        """Кадр, уже разобранный из слота, но ещё не отданный наружу."""
        return self._pending[0] if self._pending else None

    def pop_pending(self):
        if self._pending:
            return self._pending.pop(0)
        return None

    def receive(self, timeout: float):
        """Ждать кадр. Читает и в паузе тоже — см. пояснение к pause().

        Исключение — suspend_reads(): пока её срок не вышел, дальше этой
        точки не идём вовсе (ни seq(), ни read()), отдаём только то, что
        уже лежит в self._pending из более раннего чтения.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            self._expire()

            if self.yield_human:
                self.consider_slot()

            if self._pending:
                return self._deliver(self._pending.pop(0))

            now = time.monotonic()
            if now < self._reads_suspended_until:
                if now >= deadline:
                    return None
                time.sleep(min(POLL_S, self._reads_suspended_until - now, deadline - now))
                continue

            # Новая копия уже сменила номер слота. Повтор своей записи
            # в этот же заход стёр бы её раньше, чем мы успели её прочитать.
            # Это не только пауза под человека: подтверждение гостя так же
            # затирало уже лежащий в слоте хвост ответа.
            defer_nudge = False
            try:
                defer_nudge = self._store.seq() != self._seen_seq
            except ClipboardError:
                defer_nudge = False
            if not defer_nudge:
                self._maybe_nudge()
            elif self._nudge_at is not None:
                from . import app as _app
                _app.trace("gate-clip-nudge-deferred", peer=self.peer_id,
                          seen_seq=self._seen_seq)

            now = time.monotonic()
            seq = self._store.seq()
            # Пока номер молчит, буфер не открываем. Редирект как раз в это
            # время записывает ответ, и OpenClipboard ему мешает.
            if seq != self._seen_seq:
                if self._pending_seq != seq:
                    from . import app as _app
                    _app.trace("gate-clip-seq-new", peer=self.peer_id,
                               seq=seq, seen_seq=self._seen_seq)
                    self._pending_seq = seq
                    self._stale_tries = 0
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
                except ClipboardError as _exc:
                    from . import app as _app
                    _app.trace("gate-clip-read-error", peer=self.peer_id,
                               seq=seq, error=str(_exc))
                    # Номер не двигаем. Иначе кадр останется в слоте, а
                    # следующий опрос решит, что ничего не менялось.
                    if now >= deadline:
                        return None
                    time.sleep(POLL_S)
                    continue
                from . import app as _app
                if text is None:
                    _app.trace("gate-clip-read-none", peer=self.peer_id, seq=seq)
                else:
                    _app.trace("gate-clip-read-ok", peer=self.peer_id, seq=seq,
                               length=len(text), head=text[:60], dup=(text == self._last_text))
                # Пока строка прежняя, номер открыт. Следующий receive()
                # продолжит ждать: ожидание длиннее одного вызова.
                if text == self._last_text:
                    self._stale_tries += 1
                    gap = STALE_FAST_S if self._stale_tries <= STALE_FAST_TRIES else STALE_RECHECK_S
                    self._read_at = now + gap
                else:
                    self._seen_seq = seq
                    self._pending_seq = None
                    got = self._ingest(text)
                    if got is not None:
                        return self._deliver(got)
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
        if self.yielding:
            self._nudge_at = None
            self._nudge_mine = None
            return
        if self._nudge_at is None or time.monotonic() < self._nudge_at:
            return
        if self._pending:
            self._nudge_at = time.monotonic() + NUDGE_AFTER_S
            return
        mine = self._nudge_mine
        self._nudge_at = None
        self._nudge_mine = None
        if mine is None or self._my_history is not mine or self.paused:
            from . import app as _app
            _app.trace("gate-clip-nudge-skipped", peer=self.peer_id,
                      mine_is_none=mine is None,
                      history_mismatch=mine is not None and self._my_history is not mine,
                      paused=self.paused)
            return
        frames = [base64.b64decode(b64) for _, b64 in reversed(mine)]
        if not frames:
            return
        from . import app as _app
        _app.trace("gate-clip-nudge-fire", peer=self.peer_id, n_frames=len(frames))
        self._publish_many(frames, keep_history=False, schedule_nudge=False)
        hook = getattr(self, "on_nudge", None)
        if hook is not None:
            hook()

    def close(self) -> None:
        if self._paste_watch is not None:
            self._paste_watch.stop()
            self._paste_watch = None
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

    def cancel_nudge(self) -> None:
        """Снять отложенный повтор последней публикации.

        Нужно звать перед тем, как буфер будет занят чем-то, что сама
        публикация не умеет учитывать — например, файлом в bulk-режиме
        `gate.py` (`_offer_bulk()`). Иначе `_maybe_nudge()` на ближайшем
        `receive()` классическим `SetClipboardData` перепишет буфер назад
        текстом поверх только что установленного OLE-объекта раньше, чем
        редирект успеет его хоть раз передать второй стороне — найдено
        живым прогоном 28.11.2026 (`gate_bulk_oleSetClipboard_hang.md`):
        `send_snapshot()` любого кадра всегда безусловно взводит nudge
        (`transport._nudge_once = True`), и в `_offer_bulk()` это тот же
        самый анонс `OP_BULK_OFFER`, что публикуется перед settle-паузой
        (`NUDGE_AFTER_S`) и записью файла — тот же интервал, что и у
        задержки самого nudge, поэтому совпадение не на миллисекунды, а
        системное: nudge срабатывает практически сразу после того, как
        файл лёг в буфер, на каждом прогоне.
        """
        self._nudge_at = None
        self._nudge_mine = None

    def _schedule_nudge(self) -> None:
        if not isinstance(self._store, WindowsClipboard) or self.paused:
            return
        self._nudge_mine = self._my_history
        self._nudge_at = time.monotonic() + NUDGE_AFTER_S

    def _publish_many(self, frames, keep_history: bool = True,
                      schedule_nudge: bool = True, limit: int | None = None) -> None:
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
        if self._write_held():
            return
        try:
            peek = self._store.read()
        except ClipboardError:
            peek = None
        if self._refuse_human(peek):
            return
        pin = self._pin_signal
        if pin and pin not in frames:
            frames = list(frames) + [pin]
        # Повтор снимка приносит прошлую служебную ячейку вместе с данными.
        # Снимаем её и кладём одну свежую, иначе в слоте окажутся две.
        frames = [raw for raw in frames if not statcell.is_frame(raw)]
        stat = self._stats_cell()
        if not frames and stat is None:
            return
        fresh = []
        for raw in frames:
            self._out_seq += 1
            fresh.append((self._out_seq, base64.b64encode(raw).decode("ascii")))
        # Своя очередь newest-first. В слот входит вся пачка, а не только
        # последняя ячейка: иначе редирект увозил бы хвост без ACK.
        # Служебная ячейка добавляется сверху и данные не вытесняет:
        # окно кодера иначе потеряло бы последний кусок.
        mine = list(reversed(fresh))
        cap = limit if limit else HISTORY_SLOTS
        if len(mine) > cap:
            mine = mine[:cap]
        elif keep_history:
            older = [pair for pair in self._my_history if not self._cell_is_stats(pair)]
            room = HISTORY_SLOTS - len(mine)
            if room > 0:
                mine.extend(older[:room])
        if stat is not None:
            self._out_seq += 1
            mine.insert(0, (self._out_seq, base64.b64encode(stat).decode("ascii")))
        self._my_history = mine
        self._meter_out = sum(len(raw) for raw in frames)
        head = mine[0]
        merged = self._merge(mine, {})
        landed = False
        text_in = None
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
            if self._write_held() or self._refuse_human(text_in):
                self._meter_out = 0
                return
            from . import app as _app
            _app.trace("gate-clip-publish-write", peer=self.peer_id, length=len(merged))
            self._store.write(merged)
            self._note_local_write(merged)
            if text_in == self._human_released_text:
                self._human_released_text = None
            landed = True
            # Редирект иногда оставляет в слоте предыдущий снимок. Пока своей
            # новой ячейки не видно, пишем тот же номер ещё раз: новый номер
            # был бы для собеседника вторым кадром.
            if self._cell_visible(head):
                break
        else:
            if not landed and not self._write_held() and not self._refuse_human(text_in):
                from . import app as _app
                _app.trace("gate-clip-publish-write-fallback", peer=self.peer_id, length=len(merged))
                self._store.write(merged)
                self._note_local_write(merged)
                if text_in == self._human_released_text:
                    self._human_released_text = None
        if schedule_nudge:
            self._schedule_nudge()

    def _note_local_write(self, merged: str) -> None:
        """Запомнить свою запись. Ответ ждём по новому номеру, буфер не трогаем."""
        self._seen_seq = self._store.seq()
        self._last_text = merged
        self._pending_seq = None
        n = self._meter_out
        self._meter_out = 0
        picture = self.picture
        if picture is not None and n:
            picture.add_into(n)

    def _deliver(self, frame: bytes):
        picture = self.picture
        if picture is not None and frame:
            picture.add_from(len(frame))
        return frame

    def _stats_cell(self):
        cb = self.stats_frame
        if cb is None:
            return None
        try:
            raw = cb()
        except Exception:
            return None
        if not raw or not statcell.is_frame(raw):
            return None
        return raw

    def _cell_is_stats(self, pair) -> bool:
        try:
            raw = base64.b64decode(pair[1], validate=True)
        except (binascii.Error, ValueError, IndexError):
            return False
        return statcell.is_frame(raw)

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
            if self.yielding and self.yield_human and not self.paused:
                self._release_human()
            self._pending.extend(frames)
            return self._pending.pop(0)
        if self.yield_human and self._human_copy(text):
            self._hold_human(text)
        elif self.auto_pause:
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
                if statcell.is_frame(data):
                    info = statcell.unpack(data)
                    if info is not None:
                        if self.picture is not None:
                            self.picture.note_guest(info)
                        hook = self.on_stats
                        if hook is not None:
                            hook(info)
                    continue
                if self._pin_ready and self._pin_signal is not None:
                    known = self._pin_seen.get(who, ())
                    if (num, b64) not in known:
                        # Собеседник уже пишет после сигнала — повторять его незачем.
                        self._pin_signal = None
                        self._pin_ready = False
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
