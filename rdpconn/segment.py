"""Сегментация команды, которая не влезает в один кадр.

`build_frame` по-прежнему не принимает payload длиннее `MAX_PAYLOAD`: это
граница кадра, а не команды. Режет отправитель, до сборки кадра, и собирает
приёмник — до запуска и до эха.

Короткий текст уходит как раньше: payload кадра и есть команда, без заголовка.
Заголовок появляется только когда текст длиннее кадра, поэтому обычная команда
на проводе не меняется.

Заголовок куска, 8 байт:

    00 53 45 47 31 | group | index | count | данные

`00 53 45 47 31` — байты `\\x00SEG1`. Нулевой байт обычная команда не начинает,
по нему кусок отличается от текста. `group` — seq первого куска этой команды,
`index` с нуля, `count` от 2 до 255. Данных в куске не больше 232 байт, чтобы
весь payload остался в пределах 240.
"""

from __future__ import annotations

from . import config

MAGIC = b"\x00SEG1"
HEADER = len(MAGIC) + 3
CHUNK = config.MAX_PAYLOAD - HEADER


def split(raw: bytes) -> list[bytes]:
    """Нарезать уже закодированную команду. Короткий текст — один кусок как есть."""
    if len(raw) <= config.MAX_PAYLOAD:
        return [raw]
    return [raw[i:i + CHUNK] for i in range(0, len(raw), CHUNK)]


def pack(group: int, index: int, count: int, data: bytes) -> bytes:
    """Собрать payload одного куска. Длиннее кадра этот payload быть не может."""
    if not 2 <= count <= 255 or not 0 <= index < count:
        raise ValueError(f"кусок {index}/{count} вне диапазона")
    payload = MAGIC + bytes((group & 0xFF, index, count)) + data
    if len(payload) > config.MAX_PAYLOAD:
        raise ValueError(
            f"кусок {len(payload)} байт больше MAX_PAYLOAD={config.MAX_PAYLOAD}"
        )
    return payload


def unpack(payload: bytes):
    """Разобрать кусок. None — это обычная команда, а не сегмент."""
    if len(payload) < HEADER or not payload.startswith(MAGIC):
        return None
    group = payload[5]
    index = payload[6]
    count = payload[7]
    if count < 2 or index >= count:
        return None
    return group, index, count, payload[HEADER:]


class Assembly:
    """Склад кусков одной команды на приёмнике.

    Промежуточный кусок команду не запускает. Запуск — когда на месте все
    индексы от 0 до count-1. Новый `index == 0` начинает команду заново:
    повтор с начала не должен смешиваться с недособранным хвостом.
    """

    def __init__(self) -> None:
        self._group = None
        self._count = 0
        self._parts: dict[int, bytes] = {}

    def take(self, payload: bytes):
        """('plain', байты) | ('wait', None) | ('ready', вся команда)."""
        parsed = unpack(payload)
        if parsed is None:
            return "plain", payload
        group, index, count, data = parsed
        if self._group != group or self._count != count or index == 0:
            self._group = group
            self._count = count
            self._parts = {}
        self._parts[index] = data
        if all(i in self._parts for i in range(count)):
            full = b"".join(self._parts[i] for i in range(count))
            self._group = None
            self._count = 0
            self._parts = {}
            return "ready", full
        return "wait", None
