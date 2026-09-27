"""NDTP: кодек телематических пакетов и asyncio-сервер приёма.

Основные сущности::

    from ndtp import (
        NavCell, Cell, Frame, ConnRequest, NavPacket,
        FrameDecoder, encode_conn_request, encode_nav_packet, encode_result, crc16_modbus,
    )
    from ndtp.server import NdtpServer, ConnInfo
"""

from .cells import (
    CELL_NAMES,
    CELL_SIZES,
    NAV_CELL_SIZE,
    NAV_CELL_TYPE,
    Cell,
    CellParseError,
    NavCell,
    datetime_to_unix,
    parse_cell_fields,
    unix_to_datetime,
)
from .codec import (
    NPH_FLAG_REQUEST,
    NPH_RESULT,
    NPH_SGC_CONN_REQUEST,
    NPH_SND_HISTORY,
    NPH_SND_REALTIME,
    NPH_SRV_GENERIC_CONTROLS,
    NPH_SRV_NAVDATA,
    SIGNATURE,
    ConnRequest,
    Frame,
    FrameDecoder,
    Message,
    NavPacket,
    decode_frame,
    encode_conn_request,
    encode_frame,
    encode_nav_packet,
    encode_result,
)
from .crc import crc16_modbus
from .server import ConnInfo, NdtpServer

__version__ = "0.1.0"

__all__ = [
    "CELL_NAMES",
    "CELL_SIZES",
    "NAV_CELL_SIZE",
    "NAV_CELL_TYPE",
    "NPH_FLAG_REQUEST",
    "NPH_RESULT",
    "NPH_SGC_CONN_REQUEST",
    "NPH_SND_HISTORY",
    "NPH_SND_REALTIME",
    "NPH_SRV_GENERIC_CONTROLS",
    "NPH_SRV_NAVDATA",
    "SIGNATURE",
    "Cell",
    "CellParseError",
    "ConnInfo",
    "ConnRequest",
    "Frame",
    "FrameDecoder",
    "Message",
    "NavCell",
    "NavPacket",
    "NdtpServer",
    "crc16_modbus",
    "datetime_to_unix",
    "decode_frame",
    "encode_conn_request",
    "encode_frame",
    "encode_nav_packet",
    "encode_result",
    "parse_cell_fields",
    "unix_to_datetime",
]
