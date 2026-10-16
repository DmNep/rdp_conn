"""Сборка и разбор кадра: sync | type | seq | len(2) | payload | crc(2).

Раскладка полей и порядок байт длины обязаны совпадать на обеих сторонах байт
в байт; спецификация — docs/protocol.md 3.

Раскладка полей, порядок байт длины и CRC совпадают с xconn_channel.framing
байт-в-байт, и это проверяется тестом: кадр, собранный здесь, разбирается тем
разбором и наоборот. Буферу обмена не нужны ни побитовое представление, ни
преамбула для захвата тактов — кадр целиком лежит в тексте слота, поэтому здесь
только границы кадра, длина и контрольная сумма. Расходятся только удобные
конструкторы ACK/NAK — намеренно и ниже.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .crc import crc16


class FrameError(Exception):
    """Кадр принят, но неверен. Причина — в code, она уходит в NAK.

    seq — номер отвергнутого кадра, если заголовок успел прочитаться. Без него
    отвечать нечем: подтверждать отвержение, не указывая что именно отвергнуто,
    бессмысленно (docs/protocol.md 5: молча).
    """

    def __init__(self, message: str, code: int = config.NAK_CRC,
                 seq: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.seq = seq


@dataclass(frozen=True)
class Frame:
    """Разобранный кадр. CRC уже проверена, повторять не нужно."""

    type: int
    seq: int
    payload: bytes

    @property
    def type_name(self) -> str:
        return config.FRAME_TYPES.get(self.type, f"UNKNOWN({self.type:#04x})")

    def __str__(self) -> str:
        return (f"{self.type_name} seq={self.seq} len={len(self.payload)}"
                + (f" {self.payload!r}" if len(self.payload) <= 24 else ""))


def build_frame(frame_type: int, seq: int, payload: bytes = b"") -> bytes:
    """Собрать кадр. Ошибки размера — исключение, а не NAK: их допускаем мы сами."""
    if not 0 <= frame_type <= 0xFF:
        raise ValueError(f"тип кадра {frame_type} вне байта")
    if not 0 <= seq <= 0xFF:
        raise ValueError(f"seq {seq} вне байта")
    if len(payload) > config.MAX_PAYLOAD:
        raise ValueError(
            f"payload {len(payload)} байт превышает MAX_PAYLOAD="
            f"{config.MAX_PAYLOAD}, нужна сегментация на уровне приложения"
        )
    length = len(payload)
    body = bytes((config.SYNC_BYTE, frame_type, seq,
                  (length >> 8) & 0xFF, length & 0xFF)) + payload
    crc = crc16(body)
    return body + bytes(((crc >> 8) & 0xFF, crc & 0xFF))


def parse_frame(data: bytes | bytearray) -> Frame:
    """Разобрать кадр, начинающийся с sync. Бросает FrameError с причиной для NAK.

    Проверка строгая по трём причинам. Неизвестный тип обязан давать NAK, а не
    тихое игнорирование: иначе сторона с новой версией молча перестанет
    отвечать, и это неотличимо от обрыва. Длина обязана совпадать с фактическим
    размером, иначе кадр, обрезанный на границе слота, прошёл бы CRC и дал
    неверный payload. CRC проверяется последней, чтобы ошибки длины и типа не
    маскировались под контрольную сумму.
    """
    if len(data) < 1:
        raise FrameError("пустой кадр", config.NAK_LENGTH)
    if data[0] != config.SYNC_BYTE:
        raise FrameError(
            f"ожидался sync {config.SYNC_BYTE:#04x}, получен {data[0]:#04x}")
    if len(data) < 5:
        raise FrameError(f"усечённый заголовок: {len(data)} байт", config.NAK_LENGTH)

    frame_type = data[1]
    seq = data[2]
    length = (data[3] << 8) | data[4]

    if length > config.MAX_PAYLOAD:
        raise FrameError(f"длина payload {length} превышает {config.MAX_PAYLOAD}",
                         config.NAK_LENGTH, seq)
    expected = 5 + length + 2
    if len(data) != expected:
        raise FrameError(
            f"длина кадра {len(data)} не совпадает с ожидаемой {expected}",
            config.NAK_LENGTH, seq)

    payload = bytes(data[5:5 + length])
    body = bytes(data[:5 + length])
    received = (data[5 + length] << 8) | data[6 + length]
    if received != crc16(body):
        raise FrameError(f"CRC: получено {received:#06x}, "
                         f"вычислено {crc16(body):#06x}", config.NAK_CRC, seq)
    if frame_type not in config.FRAME_TYPES:
        raise FrameError(f"неизвестный тип кадра {frame_type:#04x}", config.NAK_TYPE)

    return Frame(type=frame_type, seq=seq, payload=payload)


# --- Кадры протокола --------------------------------------------------------

# Подтверждение несёт пустой payload: номер уже есть в заголовке, а дублировать
# его вторым байтом значило бы дать собеседнику два источника истины о seq.
#
# Звуковой предшественник клал в ACK байт с самим seq, а в NAK — пару (seq,
# причина). Здесь это единственный сознательный разрыв с ним, и он безопасен:
# отправитель сверяет подтверждение по type и seq из заголовка, payload ACK не
# читает, а причина NAK и так помещается в один байт. Разбор от этого не зависит
# вообще — см. проверку совместимости в тестах.
def ack(seq: int) -> bytes:
    return build_frame(config.ACK, seq)


def nak(seq: int, reason: int) -> bytes:
    """NAK с причиной одним байтом — его печатает отправитель, глядя в таблицу."""
    return build_frame(config.NAK, seq, bytes((reason,)))


def ping(seq: int) -> bytes:
    return build_frame(config.PING, seq)


def pong(seq: int) -> bytes:
    return build_frame(config.PONG, seq)


def cmd(seq: int, text: str, codec: str = config.CODEPAGE) -> bytes:
    """Команда. Перевод в конец не добавляется — его задаёт вызывающий."""
    return build_frame(config.CMD, seq, text.encode(codec, errors="replace"))


def echo(seq: int, payload: bytes) -> bytes:
    """NOTE с теми же байтами, что приняты.

    Возврат payload без перекодирования принципиален: эхо обязано подтверждать
    доехавшие байты, а не собственную расшифровку. Перекодированное эхо
    выглядело бы правильным даже при разъехавшихся кодовых страницах сторон.
    """
    return build_frame(config.NOTE, seq, payload)
