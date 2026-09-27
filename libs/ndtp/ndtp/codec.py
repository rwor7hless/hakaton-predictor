"""Кадры NDTP: NPL (15 байт) + NPH (10 байт) + тело.

Потоковый разбор (:class:`FrameDecoder`) и кодировщики кадров, которые шлёт
терминал (handshake, навигационные пакеты) и сервер (``NPH_RESULT``).
Все поля little-endian, структуры packed; CRC-16/Modbus по NPH + телу кладётся
в NPL со свапнутыми байтами.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TypeAlias

from .cells import (
    CELL_SIZES,
    NAV_CELL_TYPE,
    Cell,
    CellParseError,
    NavCell,
    encode_cells,
    iter_cells,
    parse_cell_fields,
)
from .crc import crc16_modbus

__all__ = [
    "CONN_REQUEST_SIZE",
    "NPH_FLAG_REQUEST",
    "NPH_HEADER_SIZE",
    "NPH_RESULT",
    "NPH_SGC_CONN_REQUEST",
    "NPH_SND_HISTORY",
    "NPH_SND_REALTIME",
    "NPH_SRV_GENERIC_CONTROLS",
    "NPH_SRV_NAVDATA",
    "NPL_HEADER_SIZE",
    "NPL_TYPE_NPH",
    "SIGNATURE",
    "ConnRequest",
    "Frame",
    "FrameDecoder",
    "Message",
    "NavPacket",
    "decode_frame",
    "encode_conn_request",
    "encode_frame",
    "encode_nav_packet",
    "encode_result",
]

SIGNATURE = 0x7E7E
_SIG = b"\x7e\x7e"

NPL_HEADER_SIZE = 15
NPH_HEADER_SIZE = 10
NPL_TYPE_NPH = 2

NPH_SRV_GENERIC_CONTROLS = 0
NPH_SRV_NAVDATA = 1

NPH_RESULT = 0
NPH_SGC_CONN_REQUEST = 100
NPH_SND_HISTORY = 100
NPH_SND_REALTIME = 101

NPH_FLAG_REQUEST = 0x0001

CONN_REQUEST_SIZE = 18

_NPL = struct.Struct("<HHHHBIH")
_NPH = struct.Struct("<HHHI")
_CONN = struct.Struct("<HHHIII")
_U32 = struct.Struct("<I")


def _swap16(value: int) -> int:
    return ((value & 0xFF) << 8) | (value >> 8)


@dataclass(slots=True)
class Frame:
    """Кадр NDTP с проверенным CRC, но без интерпретации тела.

    Декодер отдаёт ``Frame`` для кадров, которые не являются handshake или
    навигационным пакетом (например, ``NPH_RESULT`` или неизвестный сервис).
    Для NPL-кадров не типа NPH поля ``service_id``/``nph_type`` равны ``-1``,
    а ``body`` содержит весь NPL payload.

    Attributes:
        unit_id: NPL ``peerAddress`` (ID терминала).
        service_id: NPH ``serviceId``.
        nph_type: NPH ``type``.
        nph_flags: NPH флаги (bit0 — ``request``).
        request_id: NPH ``requestId``.
        body: Тело после NPH-заголовка.
        npl_type: NPL ``type`` (2 = NPH).
        npl_flags: NPL флаги.
        npl_request_id: NPL ``requestId``.
        raw: Кадр целиком, как пришёл по сети.
    """

    unit_id: int
    service_id: int
    nph_type: int
    nph_flags: int
    request_id: int
    body: bytes
    npl_type: int = NPL_TYPE_NPH
    npl_flags: int = 0
    npl_request_id: int = 0
    raw: bytes = field(default=b"", repr=False)

    @property
    def needs_reply(self) -> bool:
        """``True``, если отправитель ждёт ``NPH_RESULT`` (флаг ``request``)."""
        return self.npl_type == NPL_TYPE_NPH and bool(self.nph_flags & NPH_FLAG_REQUEST)

    @property
    def result_code(self) -> int | None:
        """Код ошибки из ``NPH_RESULT`` или ``None``, если кадр — не результат."""
        if self.npl_type != NPL_TYPE_NPH or self.nph_type != NPH_RESULT or len(self.body) < 4:
            return None
        return int(_U32.unpack_from(self.body, 0)[0])


@dataclass(slots=True)
class ConnRequest:
    """Handshake терминала ``NPH_SGC_CONN_REQUEST`` (service 0, type 100).

    Attributes:
        unit_id: ``peerAddress`` из тела handshake (ID терминала).
        proto_high: Старшая версия протокола.
        proto_low: Младшая версия протокола.
        max_packet_size: Максимальный размер пакета, который примет терминал.
        request_id: NPH ``requestId``.
        flags: Флаги handshake (encryption/crc/simulate).
        frame: Исходный кадр (для ответа и отладки).
    """

    unit_id: int
    proto_high: int
    proto_low: int
    max_packet_size: int
    request_id: int
    flags: int = 0
    frame: Frame | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class NavPacket:
    """Навигационный пакет (service 1 NAVDATA, type 101 REALTIME или 100 HISTORY).

    Attributes:
        unit_id: NPL ``peerAddress``.
        request_id: NPH ``requestId``.
        history: ``True`` для ``NPH_SND_HISTORY`` (type 100).
        nav: Навигационная ячейка ``G6CellNav00`` или ``None``, если её нет.
        cells: Прочие ячейки в порядке следования.
        frame: Исходный кадр (для ответа и отладки).
    """

    unit_id: int
    request_id: int
    history: bool
    nav: NavCell | None
    cells: list[Cell] = field(default_factory=list)
    frame: Frame | None = field(default=None, repr=False, compare=False)


Message: TypeAlias = ConnRequest | NavPacket | Frame


def encode_frame(
    unit_id: int,
    service_id: int,
    nph_type: int,
    body: bytes = b"",
    request_id: int = 0,
    *,
    nph_flags: int = NPH_FLAG_REQUEST,
    npl_flags: int = 0,
    npl_request_id: int = 0,
) -> bytes:
    """Собирает произвольный NDTP-кадр NPL + NPH + тело.

    Args:
        unit_id: NPL ``peerAddress``.
        service_id: NPH ``serviceId``.
        nph_type: NPH ``type``.
        body: Тело после NPH-заголовка.
        request_id: NPH ``requestId`` (u32).
        nph_flags: NPH флаги, по умолчанию ``request``.
        npl_flags: NPL флаги.
        npl_request_id: NPL ``requestId`` (u16).

    Returns:
        Готовый к отправке кадр.

    Raises:
        ValueError: Если NPH + тело не помещается в u16 ``dataSize``.
    """
    nph = _NPH.pack(service_id & 0xFFFF, nph_type & 0xFFFF, nph_flags & 0xFFFF, request_id & 0xFFFFFFFF) + body
    if len(nph) > 0xFFFF:
        raise ValueError(f"frame data too large: {len(nph)} bytes")
    npl = _NPL.pack(
        SIGNATURE,
        len(nph),
        npl_flags & 0xFFFF,
        _swap16(crc16_modbus(nph)),
        NPL_TYPE_NPH,
        unit_id & 0xFFFFFFFF,
        npl_request_id & 0xFFFF,
    )
    return npl + nph


def encode_conn_request(
    unit_id: int,
    request_id: int,
    *,
    proto_high: int = 6,
    proto_low: int = 2,
    max_packet_size: int = 65535,
    flags: int = 0,
) -> bytes:
    """Кодирует handshake ``NPH_SGC_CONN_REQUEST`` как у эмулятора.

    Args:
        unit_id: ID терминала (NPL ``peerAddress`` и поле тела).
        request_id: NPH ``requestId``.
        proto_high: Старшая версия протокола.
        proto_low: Младшая версия протокола.
        max_packet_size: Максимальный размер пакета.
        flags: Флаги handshake.

    Returns:
        Кадр handshake (15 + 10 + 18 байт).
    """
    body = _CONN.pack(proto_high, proto_low, flags, unit_id & 0xFFFFFFFF, max_packet_size, 0)
    return encode_frame(unit_id, NPH_SRV_GENERIC_CONTROLS, NPH_SGC_CONN_REQUEST, body, request_id)


def encode_nav_packet(
    unit_id: int,
    request_id: int,
    nav: NavCell,
    history: bool = False,
    extra_cells: Iterable[Cell] = (),
) -> bytes:
    """Кодирует навигационный пакет NAVDATA.

    Args:
        unit_id: ID терминала.
        request_id: NPH ``requestId``.
        nav: Навигационная ячейка (идёт первой).
        history: ``True`` — ``NPH_SND_HISTORY`` (100), иначе ``NPH_SND_REALTIME`` (101).
        extra_cells: Дополнительные ячейки после навигационной.

    Returns:
        Готовый к отправке кадр.
    """
    body = encode_cells(nav, tuple(extra_cells))
    nph_type = NPH_SND_HISTORY if history else NPH_SND_REALTIME
    return encode_frame(unit_id, NPH_SRV_NAVDATA, nph_type, body, request_id)


def encode_result(unit_id: int, request_id: int, service_id: int, error: int = 0) -> bytes:
    """Кодирует ответ сервера ``NPH_RESULT`` (type 0, тело — u32 код ошибки).

    Args:
        unit_id: ID терминала, которому адресован ответ.
        request_id: ``requestId`` запроса, на который отвечаем.
        service_id: Сервис запроса.
        error: Код ошибки, 0 — успех.

    Returns:
        Кадр ответа без флага ``request``.
    """
    return encode_frame(unit_id, service_id, NPH_RESULT, _U32.pack(error & 0xFFFFFFFF), request_id, nph_flags=0)


class FrameDecoder:
    """Потоковый декодер NDTP, один экземпляр на TCP-соединение.

    Принимает байты кусками любого размера, склеивает частичные кадры,
    пропускает мусор между кадрами (resync по сигнатуре ``0x7E7E``),
    отбрасывает кадры с неверным CRC и продолжает разбор.

    Статистика в :attr:`stats`:

    * ``frames_ok`` — кадры с верным CRC;
    * ``crc_errors`` — кадры с неверным CRC;
    * ``resyncs`` — сколько раз терялась синхронизация (мусор/битый заголовок);
    * ``unknown`` — кадры неизвестного сервиса/типа (отданы как :class:`Frame`);
    * ``bad_payload`` — кадры с верным CRC, но некорректным телом;
    * ``unknown_cells`` — ячейки неизвестного типа (остаток тела отдан сырым);
    * ``bytes_skipped`` — отброшенные байты.

    Args:
        verify_crc: Проверять ли CRC (эмулятор всегда его считает).
        max_data_size: Предельный ``dataSize``; кадры длиннее считаются мусором.

    Example:
        >>> dec = FrameDecoder()
        >>> msgs = dec.feed(chunk)  # doctest: +SKIP
    """

    def __init__(self, *, verify_crc: bool = True, max_data_size: int = 0xFFFF) -> None:
        self.verify_crc = verify_crc
        self.max_data_size = max_data_size
        self._buf = bytearray()
        self._desynced = False
        self.stats: dict[str, int] = {
            "frames_ok": 0,
            "crc_errors": 0,
            "resyncs": 0,
            "unknown": 0,
            "bad_payload": 0,
            "unknown_cells": 0,
            "bytes_skipped": 0,
        }

    @property
    def buffered(self) -> int:
        """Сколько байт ждёт продолжения кадра."""
        return len(self._buf)

    def reset(self) -> None:
        """Сбрасывает буфер (статистика сохраняется)."""
        self._buf.clear()
        self._desynced = False

    def feed(self, data: bytes | bytearray | memoryview) -> list[Message]:
        """Добавляет байты из сети и возвращает все целиком пришедшие сообщения.

        Args:
            data: Очередная порция байт (может быть пустой).

        Returns:
            Список :class:`ConnRequest`, :class:`NavPacket` или :class:`Frame`
            в порядке следования в потоке.
        """
        if data:
            self._buf += data
        out: list[Message] = []
        while True:
            frame = self._next_frame()
            if frame is None:
                return out
            out.append(self._interpret(frame))

    def _discard(self, n: int, *, resync: bool) -> None:
        if n <= 0:
            return
        del self._buf[:n]
        self.stats["bytes_skipped"] += n
        if resync and not self._desynced:
            self.stats["resyncs"] += 1
            self._desynced = True

    def _plausible(self, size: int, npl_type: int) -> bool:
        return size <= self.max_data_size and (npl_type != NPL_TYPE_NPH or size >= NPH_HEADER_SIZE)

    def _complete_valid_at(self, pos: int) -> bool:
        buf = self._buf
        if len(buf) - pos < NPL_HEADER_SIZE:
            return False
        _, size, _, crc_sw, npl_type, _, _ = _NPL.unpack_from(buf, pos)
        end = pos + NPL_HEADER_SIZE + size
        if not self._plausible(size, npl_type) or end > len(buf):
            return False
        return crc16_modbus(memoryview(buf)[pos + NPL_HEADER_SIZE : end]) == _swap16(crc_sw)

    def _later_valid_frame(self) -> int | None:
        if not self.verify_crc:
            return None
        pos = self._buf.find(_SIG, 1)
        while pos > 0:
            if self._complete_valid_at(pos):
                return pos
            pos = self._buf.find(_SIG, pos + 1)
        return None

    def _next_frame(self) -> Frame | None:
        buf = self._buf
        while True:
            idx = buf.find(_SIG)
            if idx < 0:
                keep = 1 if buf[-1:] == b"\x7e" else 0
                self._discard(len(buf) - keep, resync=True)
                return None
            self._discard(idx, resync=True)
            if len(buf) < NPL_HEADER_SIZE:
                return None
            _, size, npl_flags, crc_sw, npl_type, peer, npl_rid = _NPL.unpack_from(buf, 0)
            if not self._plausible(size, npl_type):
                self._discard(1, resync=True)
                continue
            total = NPL_HEADER_SIZE + size
            if len(buf) < total:
                later = self._later_valid_frame()
                if later is None:
                    return None
                self._discard(later, resync=True)
                continue
            data = bytes(buf[NPL_HEADER_SIZE:total])
            if self.verify_crc and crc16_modbus(data) != _swap16(crc_sw):
                self.stats["crc_errors"] += 1
                self._desynced = True
                self._discard(2, resync=False)
                continue
            raw = bytes(buf[:total])
            del buf[:total]
            self._desynced = False
            self.stats["frames_ok"] += 1
            if npl_type != NPL_TYPE_NPH:
                return Frame(peer, -1, -1, 0, 0, data, npl_type, npl_flags, npl_rid, raw)
            service_id, nph_type, nph_flags, request_id = _NPH.unpack_from(data, 0)
            return Frame(
                peer,
                service_id,
                nph_type,
                nph_flags,
                request_id,
                data[NPH_HEADER_SIZE:],
                npl_type,
                npl_flags,
                npl_rid,
                raw,
            )

    def _interpret(self, frame: Frame) -> Message:
        if frame.npl_type != NPL_TYPE_NPH:
            self.stats["unknown"] += 1
            return frame
        if frame.service_id == NPH_SRV_NAVDATA and frame.nph_type in (NPH_SND_REALTIME, NPH_SND_HISTORY):
            return self._nav_packet(frame)
        if frame.service_id == NPH_SRV_GENERIC_CONTROLS and frame.nph_type == NPH_SGC_CONN_REQUEST:
            if len(frame.body) < CONN_REQUEST_SIZE:
                self.stats["bad_payload"] += 1
                return frame
            proto_high, proto_low, flags, peer, max_size, _ = _CONN.unpack_from(frame.body, 0)
            return ConnRequest(peer, proto_high, proto_low, max_size, frame.request_id, flags, frame)
        if frame.nph_type != NPH_RESULT:
            self.stats["unknown"] += 1
        return frame

    def _nav_packet(self, frame: Frame) -> NavPacket:
        nav: NavCell | None = None
        cells: list[Cell] = []
        try:
            for cell_type, number, payload in iter_cells(frame.body):
                if cell_type == NAV_CELL_TYPE and nav is None:
                    nav = NavCell.from_bytes(payload)
                    continue
                parsed = parse_cell_fields(cell_type, payload)
                if cell_type not in CELL_SIZES:
                    self.stats["unknown_cells"] += 1
                cells.append(Cell(cell_type, number, payload, parsed))
        except CellParseError:
            self.stats["bad_payload"] += 1
        return NavPacket(
            frame.unit_id,
            frame.request_id,
            frame.nph_type == NPH_SND_HISTORY,
            nav,
            cells,
            frame,
        )


def decode_frame(data: bytes | bytearray | memoryview) -> Message:
    """Разбирает ровно один целый кадр (удобно для тестов и отладки).

    Args:
        data: Байты одного кадра без хвоста.

    Returns:
        Разобранное сообщение.

    Raises:
        ValueError: Если в данных не ровно один корректный кадр.
    """
    decoder = FrameDecoder()
    msgs = decoder.feed(data)
    if len(msgs) != 1 or decoder.buffered:
        raise ValueError(f"expected exactly one valid frame, got {len(msgs)} (+{decoder.buffered} bytes left)")
    return msgs[0]
