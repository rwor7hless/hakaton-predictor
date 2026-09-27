"""CRC-16/Modbus, которым NDTP защищает NPH-заголовок и тело кадра."""

from __future__ import annotations

__all__ = ["crc16_modbus"]


def _build_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_TABLE = _build_table()


def crc16_modbus(data: bytes | bytearray | memoryview, init: int = 0xFFFF) -> int:
    """Считает CRC-16/Modbus (poly 0xA001 reflected, init 0xFFFF, без финального XOR).

    В NPL-заголовок NDTP значение кладётся со свапнутыми байтами, то есть
    фактически big-endian: ``struct.pack(">H", crc16_modbus(nph_and_body))``.

    Args:
        data: Байты, по которым считается контрольная сумма.
        init: Начальное значение регистра; позволяет считать CRC по частям.

    Returns:
        16-битное значение CRC.

    Example:
        >>> hex(crc16_modbus(b"123456789"))
        '0x4b37'
    """
    crc = init
    table = _TABLE
    for byte in data:
        crc = (crc >> 8) ^ table[(crc ^ byte) & 0xFF]
    return crc
