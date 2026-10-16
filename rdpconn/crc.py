"""CRC-16/CCITT-FALSE — единственная контрольная сумма кадра.

Параметры (docs/protocol.md 3): полином 0x1021, инициализация 0xFFFF,
без реверса входных и выходных бит, xorout 0x0000. Контрольная сумма
строки "123456789" равна 0x29B1 — это значение проверяется тестом, оно и
отличает CCITT-FALSE от близких вариантов (XMODEM с init 0x0000,
REVERSE с реверсом бит), которые на глаз неотличимы и дают молчаливый
рассинхрон сторон.

Реализация табличная: таблица строится один раз при импорте, потому что
побитовый цикл в чистом Python на каждом кадре заметно тормозит приём.
"""

from __future__ import annotations

CRC16_POLY = 0x1021
CRC16_INIT = 0xFFFF
CRC16_XOROUT = 0x0000

# Вектор проверки реализации. Если crc16(b"123456789") вернёт не это —
# параметры CRC разъехались со спецификацией.
CRC16_CHECK = 0x29B1


def _build_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ CRC16_POLY) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_TABLE = _build_table()


def crc16(data: bytes | bytearray | memoryview, crc: int = CRC16_INIT) -> int:
    """CRC-16/CCITT-FALSE от data.

    crc принимает промежуточное значение, чтобы считать контрольную сумму
    по частям без склейки буферов. Начальное значение — CRC16_INIT.
    """
    table = _TABLE
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ table[((crc >> 8) ^ byte) & 0xFF]
    return crc ^ CRC16_XOROUT


def self_check() -> None:
    """Проверка параметров CRC при импорте модуля.

    Дешёвая и ловит рассинхрон сторон до первого кадра, а не в поле.
    """
    got = crc16(b"123456789")
    if got != CRC16_CHECK:
        raise AssertionError(
            f"CRC-16 не совпала со спецификацией: получено {got:#06x}, "
            f"ожидалось {CRC16_CHECK:#06x}"
        )


self_check()
