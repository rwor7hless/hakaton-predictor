"""Ячейки телематики NDTP: раскладка, размеры, разбор и сборка.

Каждая ячейка в теле пакета NAVDATA имеет вид ``[type: u8][number: u8][payload]``.
Размеры payload сверены с эмулятором организаторов (``GET /api/cells`` и
реальный захват трафика), см. :data:`CELL_SIZES`.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "CELL_HEADER_SIZE",
    "CELL_NAMES",
    "CELL_SIZES",
    "NAV_CELL_SIZE",
    "NAV_CELL_TYPE",
    "Cell",
    "CellParseError",
    "NavCell",
    "datetime_to_unix",
    "encode_cells",
    "iter_cells",
    "parse_cell_fields",
    "unix_to_datetime",
]

EPOCH = datetime(1970, 1, 1)

CELL_HEADER_SIZE = 2
NAV_CELL_TYPE = 0
NAV_CELL_SIZE = 26

CELL_NAMES: dict[int, str] = {
    0: "G6CellNav00",
    2: "G6CellIntSensor02",
    3: "G6CellCrown03",
    4: "G6CellIrma04",
    5: "G6CellKdm05",
    6: "G6CellIdn06",
    7: "G6CellIdn07",
    8: "G6CellUsi08",
    9: "G6CellReg09",
    10: "G6CellCan10",
    12: "G6CellRfid12",
    13: "G6CellPlo13",
    14: "G6CellBms14",
    15: "G6CellLls15",
    16: "G6CellTermo16",
    17: "G6CellAlcohol1st17",
    18: "G6CellCAN18",
    19: "G6CellGSMstations19",
    20: "G6CellM333CAN20",
    21: "G6CellAlcohol2nd21",
    22: "G6CellServerStatistics22",
    23: "G6CellTrackerStatistics23",
    100: "G6CellZipSensorData100",
}
"""Имена классов ячеек эмулятора по ``type``."""

CELL_SIZES: dict[int, int] = {
    0: 26,
    2: 26,
    3: 14,
    4: 15,
    5: 6,
    6: 9,
    7: 1,
    8: 6,
    9: 40,
    10: 37,
    12: 5,
    13: 13,
    14: 15,
    15: 50,
    16: 8,
    17: 46,
    18: 50,
    19: 40,
    20: 8,
    21: 180,
    22: 24,
    23: 16,
    100: 44,
}
"""Размер payload ячейки (без 2 байт заголовка) по ``type``; измерено на эмуляторе."""

_NAV_STRUCT = struct.Struct("<IIIBBHHHHHBB")

_Layout = tuple[tuple[str, str, int], ...]

_CELL_LAYOUTS: dict[int, _Layout] = {
    2: (
        ("an_in0", "H", 1),
        ("an_in1", "H", 1),
        ("an_in2", "H", 1),
        ("an_in3", "H", 1),
        ("di_in", "B", 1),
        ("di_out", "B", 1),
        ("di0_counter", "H", 1),
        ("di1_counter", "H", 1),
        ("di2_counter", "H", 1),
        ("di3_counter", "H", 1),
        ("odometer", "I", 1),
        ("csq", "B", 1),
        ("gprs_state", "B", 1),
        ("accel_energy", "B", 1),
        ("ext_volt", "b", 1),
    ),
    8: (
        ("det_status", "B", 1),
        ("level_mm", "H", 1),
        ("level_l", "H", 1),
        ("temperature", "B", 1),
    ),
    10: (
        ("secFlagStatus", "I", 1),
        ("allTimeEngine", "I", 1),
        ("allTrack", "I", 1),
        ("allFuelConsum", "I", 1),
        ("fuelLevel", "H", 1),
        ("speedTurnEngine", "H", 1),
        ("tEngine", "h", 1),
        ("speed", "B", 1),
        ("pressureAxis", "H", 5),
        ("flagAlarm", "I", 1),
    ),
    15: (
        ("status", "H", 1),
        ("main_float_level", "I", 1),
        ("temperature_average", "I", 1),
        ("percent_of_volume", "I", 1),
        ("total_Volume", "I", 1),
        ("weight", "I", 1),
        ("density", "I", 1),
        ("net_Standard_Volume", "I", 1),
        ("level_of_water", "I", 1),
        ("pressure", "I", 1),
        ("vapor_temperature_average", "I", 1),
        ("vapor_Weight", "I", 1),
        ("liquid_phase_Weight", "I", 1),
    ),
    16: (
        ("status", "I", 1),
        ("temp", "i", 1),
    ),
}


def _compile(layout: _Layout) -> struct.Struct:
    return struct.Struct("<" + "".join(f"{count}{code}" if count > 1 else code for _, code, count in layout))


_CELL_STRUCTS: dict[int, struct.Struct] = {t: _compile(layout) for t, layout in _CELL_LAYOUTS.items()}

for _type, _st in _CELL_STRUCTS.items():
    assert _st.size == CELL_SIZES[_type], (_type, _st.size)


class CellParseError(ValueError):
    """Тело пакета NAVDATA не раскладывается на ячейки (обрезанная ячейка)."""


def datetime_to_unix(dt: datetime) -> int:
    """Переводит наивное время (трактуется как UTC) в unix-секунды.

    Args:
        dt: Время без таймзоны, например из CSV ``2026-01-06 12:30:31.462764``.

    Returns:
        Целое число секунд с 1970-01-01 (дробная часть отбрасывается).
    """
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None) - (dt.utcoffset() or timedelta(0))
    return int((dt - EPOCH).total_seconds())


def unix_to_datetime(ts: float) -> datetime:
    """Переводит unix-секунды в наивное UTC-время («время потока»).

    Args:
        ts: Unix-время в секундах.

    Returns:
        ``datetime`` без таймзоны.
    """
    return EPOCH + timedelta(seconds=ts)


def _clamp(value: float | int, hi: int) -> int:
    if value != value or value <= 0:
        return 0
    if value >= hi:
        return hi
    return min(round(value), hi)


@dataclass(slots=True)
class NavCell:
    """Навигационная ячейка ``G6CellNav00`` (type 0, 26 байт).

    Координаты хранятся в градусах со знаком; при кодировании в протокол
    пишется ``|x| * 1e7``, а знак уходит в биты ``extraDopBit5`` (N) и
    ``extraDopBit6`` (E).

    Attributes:
        timestamp: Unix-время точки, секунды (наивное UTC «время потока»).
        lon: Долгота, градусы, восток положительный.
        lat: Широта, градусы, север положительный.
        valid: Достоверность координат (``extraDopBit7``).
        speed_avg: Средняя скорость, км/ч.
        speed_max: Максимальная скорость, км/ч.
        course: Курс, градусы 0…360.
        track: Пройденный путь, м (по модулю 65536).
        altitude: Высота над уровнем моря, м.
        nsat: Число спутников.
        pdop: PDOP.
        bat_voltage: Напряжение батареи, единица = 20 мВ.
        alarm: Тревога (``extraDopBit1``).
        sos: SOS (``extraDopBit2``).
        call_request: Запрос голосовой связи (``extraDopBit0``).
        first_power_on: Первое включение (``extraDopBit3``).
        internal_battery: Питание от встроенного АКБ (``extraDopBit4``).
    """

    timestamp: int
    lon: float
    lat: float
    valid: bool
    speed_avg: int = 0
    speed_max: int = 0
    course: int = 0
    track: int = 0
    altitude: int = 0
    nsat: int = 0
    pdop: int = 0
    bat_voltage: int = 0
    alarm: bool = False
    sos: bool = False
    call_request: bool = False
    first_power_on: bool = False
    internal_battery: bool = False

    @property
    def dt(self) -> datetime:
        """Время точки как наивный UTC ``datetime``."""
        return unix_to_datetime(self.timestamp)

    @property
    def flags(self) -> int:
        """Байт ``extraDop`` (bit0 — LSB), собранный из полей ячейки."""
        return (
            (1 if self.call_request else 0)
            | (2 if self.alarm else 0)
            | (4 if self.sos else 0)
            | (8 if self.first_power_on else 0)
            | (16 if self.internal_battery else 0)
            | (32 if self.lat >= 0 else 0)
            | (64 if self.lon >= 0 else 0)
            | (128 if self.valid else 0)
        )

    def to_bytes(self) -> bytes:
        """Кодирует ячейку в 26 байт payload (без заголовка type/number).

        Значения вне диапазона полей протокола насыщаются (clamp), ``track``
        берётся по модулю 65536.

        Returns:
            Payload ячейки ``G6CellNav00``.
        """
        return _NAV_STRUCT.pack(
            _clamp(self.timestamp, 0xFFFFFFFF),
            _clamp(abs(self.lon) * 1e7, 0xFFFFFFFF),
            _clamp(abs(self.lat) * 1e7, 0xFFFFFFFF),
            self.flags,
            _clamp(self.bat_voltage, 0xFF),
            _clamp(self.speed_avg, 0xFFFF),
            _clamp(self.speed_max, 0xFFFF),
            _clamp(self.course, 0xFFFF),
            int(self.track) & 0xFFFF,
            _clamp(self.altitude, 0xFFFF),
            _clamp(self.nsat, 0xFF),
            _clamp(self.pdop, 0xFF),
        )

    @classmethod
    def from_bytes(cls, payload: bytes | bytearray | memoryview) -> NavCell:
        """Разбирает 26 байт payload ``G6CellNav00``.

        Args:
            payload: Ровно 26 байт (или больше — лишнее игнорируется).

        Returns:
            Разобранная ячейка.

        Raises:
            CellParseError: Если байт меньше 26.
        """
        if len(payload) < NAV_CELL_SIZE:
            raise CellParseError(f"G6CellNav00 needs {NAV_CELL_SIZE} bytes, got {len(payload)}")
        (ts, lon_raw, lat_raw, flags, bat, spd_avg, spd_max, course, track, alt, nsat, pdop) = _NAV_STRUCT.unpack_from(
            payload, 0
        )
        lon = lon_raw / 1e7 if flags & 64 or lon_raw == 0 else -lon_raw / 1e7
        lat = lat_raw / 1e7 if flags & 32 or lat_raw == 0 else -lat_raw / 1e7
        return cls(
            timestamp=ts,
            lon=lon,
            lat=lat,
            valid=bool(flags & 128),
            speed_avg=spd_avg,
            speed_max=spd_max,
            course=course,
            track=track,
            altitude=alt,
            nsat=nsat,
            pdop=pdop,
            bat_voltage=bat,
            alarm=bool(flags & 2),
            sos=bool(flags & 4),
            call_request=bool(flags & 1),
            first_power_on=bool(flags & 8),
            internal_battery=bool(flags & 16),
        )

    def to_cell_bytes(self, number: int = 0) -> bytes:
        """Кодирует ячейку вместе с заголовком ``[0][number]``.

        Args:
            number: Индекс ячейки среди ячеек того же типа.

        Returns:
            28 байт: заголовок + payload.
        """
        return bytes((NAV_CELL_TYPE, number & 0xFF)) + self.to_bytes()


def parse_cell_fields(cell_type: int, payload: bytes | bytearray | memoryview) -> dict[str, Any] | None:
    """Разбирает payload ячейки известной раскладки в словарь полей.

    Поддержаны ``G6CellIntSensor02``, ``G6CellUsi08``, ``G6CellCan10``,
    ``G6CellLls15``, ``G6CellTermo16``; имена ключей совпадают с именами полей
    JSON эмулятора. Массивы (``pressureAxis``) возвращаются списками.

    Args:
        cell_type: Тип ячейки.
        payload: Байты payload без заголовка.

    Returns:
        Словарь полей или ``None``, если раскладка типа неизвестна либо
        payload короче нужного.
    """
    st = _CELL_STRUCTS.get(cell_type)
    if st is None or len(payload) < st.size:
        return None
    values = st.unpack_from(payload, 0)
    out: dict[str, Any] = {}
    pos = 0
    for name, _, count in _CELL_LAYOUTS[cell_type]:
        if count == 1:
            out[name] = values[pos]
        else:
            out[name] = list(values[pos : pos + count])
        pos += count
    return out


def _pack_cell_fields(cell_type: int, fields: dict[str, Any]) -> bytes:
    st = _CELL_STRUCTS[cell_type]
    values: list[int] = []
    for name, _, count in _CELL_LAYOUTS[cell_type]:
        v = fields.get(name, 0)
        if count == 1:
            values.append(int(v))
        else:
            seq = list(v) if v else []
            values.extend(int(x) for x in (seq + [0] * count)[:count])
    return st.pack(*values)


@dataclass(slots=True)
class Cell:
    """Ячейка телематики, отличная от навигационной.

    Attributes:
        type: Тип ячейки (см. :data:`CELL_NAMES`).
        number: Индекс среди ячеек того же типа в пакете.
        raw: Payload ячейки как есть (без заголовка).
        fields: Разобранные поля для известных раскладок, иначе ``None``.
    """

    type: int
    number: int = 0
    raw: bytes = b""
    fields: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        """Имя класса ячейки эмулятора, например ``G6CellUsi08``."""
        return CELL_NAMES.get(self.type, f"Unknown{self.type}")

    def payload(self) -> bytes:
        """Возвращает payload для кодирования.

        Приоритет: ``raw`` → упаковка ``fields`` (для известных раскладок) →
        нули длины :data:`CELL_SIZES` для типа.

        Returns:
            Байты payload без заголовка.

        Raises:
            ValueError: Если тип неизвестен и ``raw`` не задан.
        """
        if self.raw:
            return bytes(self.raw)
        if self.fields is not None and self.type in _CELL_STRUCTS:
            return _pack_cell_fields(self.type, self.fields)
        size = CELL_SIZES.get(self.type)
        if size is None:
            raise ValueError(f"cell type {self.type} has unknown size and no raw payload")
        return bytes(size)

    def to_bytes(self) -> bytes:
        """Кодирует ячейку вместе с заголовком ``[type][number]``.

        Returns:
            Заголовок и payload ячейки.
        """
        return bytes((self.type & 0xFF, self.number & 0xFF)) + self.payload()


def iter_cells(body: bytes | bytearray | memoryview) -> Iterator[tuple[int, int, bytes]]:
    """Итерирует ячейки тела пакета NAVDATA.

    Для неизвестного типа остаток тела отдаётся одной «ячейкой» целиком —
    дальше разделить поток невозможно, так как размер неизвестен.

    Args:
        body: Тело NPH-пакета (после 10 байт NPH-заголовка).

    Yields:
        Кортежи ``(type, number, payload)``.

    Raises:
        CellParseError: Если ячейка известного типа обрезана.
    """
    view = memoryview(body)
    pos = 0
    end = len(view)
    while pos < end:
        if end - pos < CELL_HEADER_SIZE:
            raise CellParseError(f"dangling byte at offset {pos}")
        cell_type = view[pos]
        number = view[pos + 1]
        pos += CELL_HEADER_SIZE
        size = CELL_SIZES.get(cell_type)
        if size is None:
            yield cell_type, number, bytes(view[pos:end])
            return
        if end - pos < size:
            raise CellParseError(f"cell type {cell_type} truncated: need {size}, have {end - pos}")
        yield cell_type, number, bytes(view[pos : pos + size])
        pos += size


def encode_cells(nav: NavCell | None, extra_cells: tuple[Cell, ...] | list[Cell] = ()) -> bytes:
    """Собирает тело пакета NAVDATA: навигационная ячейка первой, затем остальные.

    Args:
        nav: Навигационная ячейка или ``None``.
        extra_cells: Прочие ячейки в нужном порядке.

    Returns:
        Байты тела пакета.
    """
    parts = [nav.to_cell_bytes()] if nav is not None else []
    parts.extend(cell.to_bytes() for cell in extra_cells)
    return b"".join(parts)
