"""Файл через буфер обмена — как делает Проводник, а не как tx/rx.

Весь остальной канал (`clipboard.py`, `framing.py`, `gate.py`, `coder.py`)
сидит на `CF_UNICODETEXT`: кадр — это печатаемый текст в обычном текстовом
буфере. Измерено (2026-11-26, см. vault/память проекта): голый текстовый
буфер — 2–4.5 КБ/с, протокол `gate.py` на пике ячеек — 13–26 КБ/с. Но когда
пользователь скопировал файл в Проводнике через ту же самую RDP-сессию —
десятки мегабайт в секунду. Разница на три порядка — потому что Проводник
кладёт в буфер не текст, а настоящий файловый объект: `FileGroupDescriptorW`
(список имя+размер) и `FileContents` (сам файл, по одному на поток —
`IStream`, читается чанками, то есть лениво: ничего не передаётся, пока
получатель не попросит кусок). RDP особо поддерживает именно эту пару
форматов (MS-RDPECLIP), а обычный `CF_UNICODETEXT` — лишь то, что
RDP-редирект переносит заодно, без какой-либо специальной скорости.

Отправка — `SHCreateDataObject` + `OleSetClipboard`, то же самое, что делает
Проводник при обычном Ctrl+C на файле (shell сам собирает рабочий объект с
delay-render по пути на диске — реализовывать `FileContents` самим не нужно).
`OleFlushClipboard()` после установки обязателен: без него данные живут
только пока жив процесс-отправитель (OLE держит ссылку на владельца, а не
копирует формат сразу), а отправитель здесь — короткий процесс.

Приём — `OleGetClipboard` + `GetData` по очереди для `FileGroupDescriptorW`
(список) и `FileContents(i)` (сам поток, `IStream::Read` чанками). Оба формата
не предлагает наш же `SHCreateDataObject`-объект на том же компьютере без
RDP (сам факт того, что файл локальный, делает `FileContents`
бессмысленным — получатель и так может открыть путь напрямую). Это не баг
приёма: `FileContents` реально появляется только когда объект прошёл через
RDP-редирект, который сам читает путь с диска на стороне отправителя и
отдаёт байты по сети как настоящий поток. Поэтому обратную связь — дошло
или нет — проверяют только живым прогоном через вторую машину, локальная
проверка на одной машине не докажет ничего о реальном канале.

Zero dependencies: ничего, кроме `ctypes` (stdlib) — ни `pywin32`, ни
`comtypes`. COM-интерфейсы (`IDataObject`, `IStream`, `IEnumFORMATETC`)
вызываются по сырому vtable (таблица указателей на функции по фиксированным
индексам — IUnknown всегда 0..2, остальное — в порядке объявления интерфейса
в MSDN), без единой обёртки.
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import app

ole32 = ctypes.OleDLL("ole32")
shell32 = ctypes.windll.shell32
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ctypes по умолчанию считает аргументы и возврат c_int (32 бита): указатель на
# x64 (8 байт) в таком вызове молча обрезается — либо AccessViolation сразу,
# либо тихая порча памяти чуть позже, в произвольном месте. Для всего, что
# возвращает или принимает указатель/HRESULT/размер, типы объявлены явно —
# один раз на модуль, а не на каждый вызов.
shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
shell32.ILCreateFromPathW.restype = ctypes.c_void_p
shell32.ILFindLastID.argtypes = [ctypes.c_void_p]
shell32.ILFindLastID.restype = ctypes.c_void_p
shell32.ILClone.argtypes = [ctypes.c_void_p]
shell32.ILClone.restype = ctypes.c_void_p
shell32.ILRemoveLastID.argtypes = [ctypes.c_void_p]
shell32.ILRemoveLastID.restype = ctypes.c_int
shell32.ILFree.argtypes = [ctypes.c_void_p]
shell32.ILFree.restype = None
shell32.SHCreateDataObject.restype = ctypes.c_long
user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
user32.RegisterClipboardFormatW.restype = ctypes.c_uint
user32.GetClipboardFormatNameW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClipboardFormatNameW.restype = ctypes.c_int
user32.GetClipboardSequenceNumber.restype = ctypes.c_uint
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = ctypes.c_int
kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.restype = ctypes.c_size_t

# Инициализация COM — на весь процесс, один раз при импорте модуля, а не в
# каждой функции и не в CLI-обвязке: send_file/recv_file/list_clipboard_files
# вызывают OLE-функции независимо от того, пришли ли они из CLI этого модуля
# или импортированы напрямую (как в tools/bench_file_clipboard.py) — без
# этого первый же вызов падает CO_E_NOTINITIALIZED. OleInitialize безопасно
# звать больше одного раза (считает ссылки, при повторном вызове просто
# вернёт S_FALSE), поэтому дублирование с чужим CoInitialize в хост-процессе
# не страшно.
ole32.OleInitialize(None)


class ClipboardFileError(Exception):
    """Любая ошибка COM/shell при работе с файлом в буфере. str() — для stderr."""


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise ClipboardFileError(f"{what}: HRESULT 0x{hr & 0xffffffff:08x}")


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_IID_IDataObject = _GUID(0x0000010e, 0x0000, 0x0000,
                         (ctypes.c_ubyte * 8)(0xC0, 0, 0, 0, 0, 0, 0, 0x46))


class _FORMATETC(ctypes.Structure):
    _fields_ = [
        ("cfFormat", ctypes.c_ushort),
        ("_pad", ctypes.c_ushort),
        ("ptd", ctypes.c_void_p),
        ("dwAspect", ctypes.c_ulong),
        ("lindex", ctypes.c_long),
        ("tymed", ctypes.c_ulong),
    ]


class _STGMEDIUM(ctypes.Structure):
    _fields_ = [
        ("tymed", ctypes.c_ulong),
        ("data", ctypes.c_void_p),
        ("pUnkForRelease", ctypes.c_void_p),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


class _FILEDESCRIPTORW(ctypes.Structure):
    # Раскладка строго как в shlobj_core.h: смещения трогать нельзя, иначе
    # cFileName съедет и накрошит из хвоста предыдущего поля.
    _fields_ = [
        ("dwFlags", ctypes.c_ulong),
        ("clsid", ctypes.c_byte * 16),
        ("sizel_cx", ctypes.c_long), ("sizel_cy", ctypes.c_long),
        ("pointl_x", ctypes.c_long), ("pointl_y", ctypes.c_long),
        ("dwFileAttributes", ctypes.c_ulong),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("nFileSizeHigh", ctypes.c_ulong),
        ("nFileSizeLow", ctypes.c_ulong),
        ("cFileName", ctypes.c_wchar * 260),
    ]


_TYMED_HGLOBAL = 1
_TYMED_ISTREAM = 4
_DVASPECT_CONTENT = 1


def _vtable_call(interface_ptr, index: int, restype, argtypes, *args):
    """Вызов метода чужого COM-интерфейса по индексу в vtable.

    IUnknown всегда 0=QueryInterface, 1=AddRef, 2=Release — метод интерфейса,
    который мы реально вызываем, идёт дальше в порядке объявления: GetData у
    IDataObject — 3-й, Read у IStream — тоже 3-й (ISequentialStream не несёт
    своих 0..2, у него нет предка кроме IUnknown).
    """
    vtable = ctypes.cast(interface_ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    method_addr = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
    func = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(method_addr)
    return func(interface_ptr, *args)


def _get_data(ppdo, cf: int, lindex: int, tymeds: tuple[int, ...]) -> _STGMEDIUM:
    """GetData с перебором tymed: один и тот же формат разные источники могут
    предлагать по-разному (HGLOBAL у одного, ISTREAM у другого) — не угадывать
    заранее, а спросить both и взять тот, что ответил без ошибки.
    """
    med = _STGMEDIUM()
    last_err = None
    for tymed in tymeds:
        fmt = _FORMATETC(cfFormat=cf, _pad=0, ptd=None,
                         dwAspect=_DVASPECT_CONTENT, lindex=lindex, tymed=tymed)
        hr = _vtable_call(ppdo, 3, ctypes.c_long,
                          [ctypes.POINTER(_FORMATETC), ctypes.POINTER(_STGMEDIUM)],
                          ctypes.byref(fmt), ctypes.byref(med))
        if hr == 0:
            return med
        last_err = hr
    _check(last_err, f"GetData(cf={cf}, lindex={lindex})")
    return med  # недостижимо, _check уже бросил


@dataclass
class FileEntry:
    name: str
    size: int


def send_files(paths: list[str | Path], *, flush_retries: int = 5, flush_retry_wait: float = 0.2) -> None:
    """Положить несколько файлов в буфер разом — `FileGroupDescriptorW.cItems > 1`,
    как при выделении нескольких файлов в Проводнике и Ctrl+C.

    Все файлы обязаны лежать в одной папке: `SHCreateDataObject` принимает
    ровно один `pidl` папки-родителя и массив child-`pidl` относительно
    него — это ограничение самого shell API (PIDL ребёнка однозначен только
    внутри своего родителя), не наше; файлы из разных папок этим вызовом не
    собрать в один объект буфера.

    flush_retries — см. докстринг `send_file()`: то же самое, тот же retry.
    """
    resolved = [str(Path(p).resolve()) for p in paths]
    if not resolved:
        raise ClipboardFileError("send_files() вызван без путей")
    parents = {str(Path(p).parent) for p in resolved}
    if len(parents) > 1:
        raise ClipboardFileError(
            f"все файлы должны быть в одной папке (ограничение SHCreateDataObject), "
            f"получили папки: {sorted(parents)}")
    pidls_full = [shell32.ILCreateFromPathW(p) for p in resolved]
    try:
        for p, pidl in zip(resolved, pidls_full):
            if not pidl:
                raise ClipboardFileError(f"ILCreateFromPathW({p!r}) вернул NULL — файла нет?")
        children = [shell32.ILFindLastID(pidl) for pidl in pidls_full]
        pidl_folder = shell32.ILClone(pidls_full[0])
        try:
            shell32.ILRemoveLastID(pidl_folder)
            ppv = ctypes.c_void_p()
            apidl = (ctypes.c_void_p * len(children))(*children)
            hr = shell32.SHCreateDataObject(
                ctypes.c_void_p(pidl_folder), len(children), apidl, None,
                ctypes.byref(_IID_IDataObject), ctypes.byref(ppv))
            _check(hr, "SHCreateDataObject")
            hr = ole32.OleSetClipboard(ppv)
            _check(hr, "OleSetClipboard")
            for attempt in range(flush_retries):
                try:
                    ole32.OleFlushClipboard()
                    break
                except OSError:
                    # ole32 — OleDLL: при неуспехе бросает OSError сам, раньше,
                    # чем успел бы сработать _check. CLIPBRD_E_CANT_OPEN —
                    # гонка за чужой короткой блокировкой буфера, не настоящая
                    # ошибка; без повторной попытки это была бы та же гонка,
                    # что OpenClipboard уже 20+ лет ловит у любого приложения.
                    if attempt == flush_retries - 1:
                        raise
                    time.sleep(flush_retry_wait)
        finally:
            shell32.ILFree(pidl_folder)
    finally:
        for pidl in pidls_full:
            if pidl:
                shell32.ILFree(pidl)
    app.trace("fileclip-send", paths=resolved, count=len(resolved),
             total_size=sum(os.path.getsize(p) for p in resolved))


def send_file(path: str | Path, *, flush_retries: int = 5, flush_retry_wait: float = 0.2) -> None:
    """Положить один файл в буфер как Проводник: Ctrl+C на одном файле.

    Тонкая обёртка над `send_files([path])` — один файл - частный случай
    нескольких, не отдельный путь в коде.
    """
    send_files([path], flush_retries=flush_retries, flush_retry_wait=flush_retry_wait)


def sequence_number() -> int:
    """Счётчик изменений буфера. Растёт на любую запись — свою и чужую (как
    у `WindowsClipboard.seq()` в clipboard.py, тот же принцип и то же API)."""
    return user32.GetClipboardSequenceNumber()


def _ole_get_clipboard(*, open_retries: int = 10, open_retry_wait: float = 0.1) -> ctypes.c_void_p:
    """`OleGetClipboard` с retry — та же гонка за `OpenClipboard`, что у
    `OleFlushClipboard()` в `send_files()`, только на стороне приёма: другой
    процесс (сам RDP-редиректор или наш же сосед) может на мгновение держать
    буфер открытым сразу после публикации нового содержимого. Пойман живьём:
    `list_clipboard_files()` падал `CLIPBRD_E_CANT_OPEN` сразу после того, как
    `wait_for_new_file()` увидел смену `seq()` — то есть ровно тогда, когда
    редиректор мог ещё не закрыть буфер после своей записи.
    """
    ppdo = ctypes.c_void_p()
    last_err: OSError | None = None
    for attempt in range(open_retries):
        try:
            hr = ole32.OleGetClipboard(ctypes.byref(ppdo))
            _check(hr, "OleGetClipboard")
            return ppdo
        except OSError as exc:
            last_err = exc
            if attempt == open_retries - 1:
                raise
            time.sleep(open_retry_wait)
    raise last_err  # недостижимо


def list_clipboard_files(*, open_retries: int = 10, open_retry_wait: float = 0.1) -> list[FileEntry]:
    """FileGroupDescriptorW: имена и размеры, без скачивания содержимого."""
    cf_fgd = user32.RegisterClipboardFormatW("FileGroupDescriptorW")
    ppdo = _ole_get_clipboard(open_retries=open_retries, open_retry_wait=open_retry_wait)
    med = _get_data(ppdo, cf_fgd, -1, (_TYMED_HGLOBAL, _TYMED_ISTREAM))
    rec = ctypes.sizeof(_FILEDESCRIPTORW)
    if med.tymed == _TYMED_ISTREAM:
        data = _read_stream_all(med.data)
        count = int.from_bytes(data[:4], "little")
        return [_parse_descriptor(data[4 + i * rec:4 + (i + 1) * rec]) for i in range(count)]
    ptr = kernel32.GlobalLock(med.data)
    try:
        count = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint))[0]
        base = ptr + ctypes.sizeof(ctypes.c_uint)
        return [_descriptor_at(base + i * rec) for i in range(count)]
    finally:
        kernel32.GlobalUnlock(med.data)


def _parse_descriptor(raw: bytes) -> FileEntry:
    fd = _FILEDESCRIPTORW.from_buffer_copy(raw)
    return FileEntry(name=fd.cFileName, size=(fd.nFileSizeHigh << 32) | fd.nFileSizeLow)


def _descriptor_at(addr: int) -> FileEntry:
    fd = _FILEDESCRIPTORW.from_address(addr)
    return FileEntry(name=fd.cFileName, size=(fd.nFileSizeHigh << 32) | fd.nFileSizeLow)


def _read_stream_all(stream, chunk: int = 65536) -> bytes:
    buf = ctypes.create_string_buffer(chunk)
    read = ctypes.c_ulong(0)
    out = bytearray()
    while True:
        hr = _vtable_call(stream, 3, ctypes.c_long,
                          [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)],
                          buf, chunk, ctypes.byref(read))
        if read.value == 0:
            break
        out.extend(buf.raw[:read.value])
        if hr != 0:
            break
    return bytes(out)


@dataclass
class RecvResult:
    bytes_total: int
    first_byte_s: float
    total_s: float
    samples: list[tuple[float, int]] = field(default_factory=list)


def recv_file(index: int, out_path: str | Path, *, chunk: int = 524288,
             formatetc_retries: int = 15, formatetc_retry_wait: float = 0.1,
             open_retries: int = 10, open_retry_wait: float = 0.1) -> RecvResult:
    """FileContents(index) -> IStream, читаем чанками в out_path.

    samples — (perf_counter, len(чанка)) на каждое успешное Read; тем же
    способом, что `bench_ceiling_live.py._peak_rate`, по ним можно посчитать
    пиковую скорость отдельно от средней — здесь тоже бывают всплеск/плато,
    раз это RDP-канал, а не локальный диск.

    formatetc_retries — GetData(FileContents) замечен падающим с
    DV_E_FORMATETC сразу после того, как `wait_for_new_file` увидел смену
    seq(): RDP-редиректор публикует FileGroupDescriptorW (список) чуть раньше,
    чем регистрирует саму потоковую форму для FileContents — короткая гонка
    на стороне редиректора, не наша логика (FileGroupDescriptorW к этому
    моменту уже читается без проблем). Без повтора рывок сразу после
    появления файла в буфере не прочитать.
    """
    cf_contents = user32.RegisterClipboardFormatW("FileContents")
    ppdo = _ole_get_clipboard(open_retries=open_retries, open_retry_wait=open_retry_wait)
    started = time.perf_counter()
    med = None
    last_err: ClipboardFileError | None = None
    for attempt in range(formatetc_retries):
        try:
            med = _get_data(ppdo, cf_contents, index, (_TYMED_ISTREAM, _TYMED_HGLOBAL))
            break
        except ClipboardFileError as exc:
            last_err = exc
            app.trace("fileclip-recv-retry", attempt=attempt, error=str(exc))
            time.sleep(formatetc_retry_wait)
    if med is None:
        raise last_err
    samples: list[tuple[float, int]] = []
    total = 0
    out_path = str(out_path)
    with open(out_path, "wb") as f:
        if med.tymed == _TYMED_HGLOBAL:
            size = kernel32.GlobalSize(med.data)
            ptr = kernel32.GlobalLock(med.data)
            try:
                f.write(ctypes.string_at(ptr, size))
                total = size
                samples.append((time.perf_counter(), size))
            finally:
                kernel32.GlobalUnlock(med.data)
        else:
            stream = med.data
            buf = ctypes.create_string_buffer(chunk)
            read = ctypes.c_ulong(0)
            while True:
                hr = _vtable_call(stream, 3, ctypes.c_long,
                                  [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)],
                                  buf, chunk, ctypes.byref(read))
                if read.value == 0:
                    break
                f.write(buf.raw[:read.value])
                total += read.value
                samples.append((time.perf_counter(), read.value))
                if hr != 0:
                    break
    done = time.perf_counter()
    first_byte_s = (samples[0][0] - started) if samples else -1.0
    result = RecvResult(bytes_total=total, first_byte_s=first_byte_s,
                        total_s=done - started, samples=samples)
    app.trace("fileclip-recv", path=out_path, bytes=total,
             first_byte_s=round(result.first_byte_s, 4), total_s=round(result.total_s, 4))
    return result


def wait_for_new_file(since_seq: int, *, poll_interval: float = 0.05, timeout: float = 120.0) -> int:
    """Спать, пока в буфере не появится новый FileGroupDescriptorW.

    Проверяем и смену seq() (дешёвая проверка на каждый тик), и реально ли
    это файл (не любое другое изменение буфера — тот же текстовый обмен
    `gate`/`coder` на этой же машине тоже меняет seq()). Возвращает новый seq.
    """
    deadline = time.monotonic() + timeout
    cf_fgd = user32.RegisterClipboardFormatW("FileGroupDescriptorW")
    while time.monotonic() < deadline:
        seq = sequence_number()
        if seq != since_seq:
            if user32.IsClipboardFormatAvailable(cf_fgd):
                return seq
            since_seq = seq
        time.sleep(poll_interval)
    raise TimeoutError(f"файл не появился в буфере за {timeout:g} с")


user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
user32.IsClipboardFormatAvailable.restype = ctypes.c_int


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="rdpconn.fileclip")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_send = sub.add_parser("send", help="положить файл(ы) в буфер")
    p_send.add_argument("paths", nargs="+")

    p_list = sub.add_parser("list", help="показать файлы, сейчас лежащие в буфере")

    p_recv = sub.add_parser("recv", help="забрать файл(ы) из буфера в папку")
    p_recv.add_argument("out_dir")
    p_recv.add_argument("--chunk", type=int, default=524288)
    p_recv.add_argument("--formatetc-retries", type=int, default=15)
    p_recv.add_argument("--formatetc-retry-wait", type=float, default=0.1)
    p_recv.add_argument("--open-retries", type=int, default=10)
    p_recv.add_argument("--open-retry-wait", type=float, default=0.1)

    p_watch = sub.add_parser("watch", help="ждать новый файл и забрать его")
    p_watch.add_argument("out_dir")
    p_watch.add_argument("--chunk", type=int, default=524288)
    p_watch.add_argument("--timeout", type=float, default=120.0)
    p_watch.add_argument("--poll-interval", type=float, default=0.05)
    p_watch.add_argument("--formatetc-retries", type=int, default=15)
    p_watch.add_argument("--formatetc-retry-wait", type=float, default=0.1)
    p_watch.add_argument("--open-retries", type=int, default=10)
    p_watch.add_argument("--open-retry-wait", type=float, default=0.1)

    args = parser.parse_args(argv)
    if args.mode == "send":
        send_files(args.paths)
        app.say(f"положил в буфер ({len(args.paths)}): {', '.join(args.paths)}")
    elif args.mode == "list":
        for e in list_clipboard_files():
            app.say(f"{e.name}: {e.size} байт")
    elif args.mode in ("recv", "watch"):
        if args.mode == "watch":
            wait_for_new_file(sequence_number(), poll_interval=args.poll_interval,
                              timeout=args.timeout)
        entries = list_clipboard_files(open_retries=args.open_retries,
                                       open_retry_wait=args.open_retry_wait)
        os.makedirs(args.out_dir, exist_ok=True)
        for i, e in enumerate(entries):
            out_path = os.path.join(args.out_dir, e.name)
            result = recv_file(i, out_path, chunk=args.chunk,
                              formatetc_retries=args.formatetc_retries,
                              formatetc_retry_wait=args.formatetc_retry_wait,
                              open_retries=args.open_retries,
                              open_retry_wait=args.open_retry_wait)
            rate = result.bytes_total / result.total_s if result.total_s > 0 else 0.0
            app.say(f"[{i}] {e.name}: {result.bytes_total} байт за {result.total_s:.3f} с "
                   f"(первый байт {result.first_byte_s:.3f} с, {rate:.0f} Б/с)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
