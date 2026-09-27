"""Asyncio TCP-сервер приёма NDTP от терминалов (эмулятора, replayer'а)."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .codec import ConnRequest, Frame, FrameDecoder, Message, encode_result

__all__ = ["ConnInfo", "DisconnectCallback", "MessageCallback", "NdtpServer"]

log = logging.getLogger("ndtp.server")

_STAT_KEYS = ("frames_ok", "crc_errors", "resyncs", "unknown", "bad_payload", "unknown_cells", "bytes_skipped")


@dataclass(eq=False)
class ConnInfo:
    """Состояние одного TCP-соединения терминала.

    Экземпляр хешируется по идентичности и годится как ключ словаря.

    Attributes:
        conn_id: Порядковый номер соединения в пределах сервера.
        peer: Адрес клиента ``host:port``.
        connected_at: Время подключения, unix-секунды (wall clock).
        unit_id: ID терминала из handshake или первого пакета; ``None`` до них.
        last_seen: Время последнего приёма данных, unix-секунды.
        messages: Сколько сообщений разобрано на соединении.
        replies: Сколько ``NPH_RESULT`` отправлено.
        decoder: Декодер потока этого соединения.
    """

    conn_id: int
    peer: str
    connected_at: float
    unit_id: int | None = None
    last_seen: float = 0.0
    messages: int = 0
    replies: int = 0
    decoder: FrameDecoder = field(default_factory=FrameDecoder, repr=False)

    @property
    def stats(self) -> dict[str, int]:
        """Статистика декодера соединения (см. :class:`~ndtp.codec.FrameDecoder`)."""
        return self.decoder.stats


MessageCallback = Callable[[ConnInfo, Message], Awaitable[Any] | Any]
DisconnectCallback = Callable[[ConnInfo], Awaitable[Any] | Any]


class NdtpServer:
    """TCP-сервер NDTP на asyncio.

    На каждое соединение заводится свой :class:`~ndtp.codec.FrameDecoder`.
    Для каждого разобранного сообщения вызывается ``on_message``; на запросы с
    флагом ``request`` сервер сразу отвечает ``NPH_RESULT`` (код 0). Ошибки в
    коллбэках, мусор и обрывы соединений логируются и не роняют сервер.

    Коллбэки могут быть обычными функциями или корутинами. Сообщения одного
    соединения обрабатываются строго последовательно.

    Args:
        host: Адрес для прослушивания.
        port: TCP-порт; ``0`` — выбрать свободный (см. :attr:`port`).
        on_message: ``(conn_info, msg)`` для каждого сообщения.
        on_disconnect: ``(conn_info)`` после закрытия соединения.
        on_connect: ``(conn_info)`` сразу после подключения.
        reply: Отвечать ли ``NPH_RESULT`` на запросы.
        idle_timeout: Закрывать соединение без данных дольше N секунд; ``None`` — не закрывать.
        read_size: Размер порции чтения из сокета.
        max_write_buffer: Если клиент не читает ответы и буфер отправки больше
            этого значения, ответы перестают ставиться в очередь.

    Example:
        >>> async def handle(conn, msg):
        ...     print(conn.unit_id, msg)
        >>> server = NdtpServer("0.0.0.0", 9201, handle)  # doctest: +SKIP
        >>> await server.start()  # doctest: +SKIP
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9201,
        on_message: MessageCallback | None = None,
        on_disconnect: DisconnectCallback | None = None,
        *,
        on_connect: DisconnectCallback | None = None,
        reply: bool = True,
        idle_timeout: float | None = None,
        read_size: int = 65536,
        max_write_buffer: int = 1 << 20,
    ) -> None:
        self.host = host
        self._port = port
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.on_connect = on_connect
        self.reply = reply
        self.idle_timeout = idle_timeout
        self.read_size = read_size
        self.max_write_buffer = max_write_buffer
        self._server: asyncio.Server | None = None
        self._ids = itertools.count(1)
        self._conns: dict[int, tuple[ConnInfo, asyncio.StreamWriter]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed_stats: dict[str, int] = dict.fromkeys(_STAT_KEYS, 0)
        self._counters: dict[str, int] = {
            "connections_total": 0,
            "messages_total": 0,
            "replies_total": 0,
            "replies_dropped": 0,
            "callback_errors": 0,
        }

    @property
    def port(self) -> int:
        """Фактический порт (полезно при ``port=0``)."""
        if self._server is not None and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self._port

    @property
    def is_serving(self) -> bool:
        """Принимает ли сервер новые соединения."""
        return self._server is not None and self._server.is_serving()

    @property
    def connections(self) -> list[ConnInfo]:
        """Активные соединения."""
        return [info for info, _ in self._conns.values()]

    @property
    def stats(self) -> dict[str, int]:
        """Сводная статистика: счётчики сервера и суммарная статистика декодеров."""
        out = dict(self._closed_stats)
        for info, _ in self._conns.values():
            for key in _STAT_KEYS:
                out[key] += info.decoder.stats.get(key, 0)
        out.update(self._counters)
        out["connections_active"] = len(self._conns)
        return out

    async def start(self) -> None:
        """Начинает слушать порт; повторный вызов ничего не делает."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle, self.host, self._port, reuse_address=True)
        log.info("NDTP server listening on %s:%s", self.host, self.port)

    async def serve_forever(self) -> None:
        """Запускает сервер (если ещё не запущен) и обслуживает до отмены."""
        await self.start()
        assert self._server is not None
        try:
            await self._server.serve_forever()
        finally:
            await self.stop()

    async def stop(self, timeout: float = 5.0) -> None:
        """Останавливает приём, закрывает все соединения и ждёт их обработчики.

        Args:
            timeout: Сколько ждать завершения обработчиков перед их отменой.
        """
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for _, writer in list(self._conns.values()):
            writer.close()
        tasks = [t for t in self._tasks if not t.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=timeout)
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), timeout)
            except TimeoutError:
                log.warning("NDTP server did not close in %.1fs", timeout)
            log.info("NDTP server on port %s stopped", self._port)

    async def __aenter__(self) -> NdtpServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _call(self, callback: Callable[..., Any] | None, *args: Any) -> None:
        if callback is None:
            return
        try:
            result = callback(*args)
            if inspect.isawaitable(result):
                await result
        except Exception:
            self._counters["callback_errors"] += 1
            log.exception("NDTP callback %r failed", callback)

    def _send_reply(self, info: ConnInfo, writer: asyncio.StreamWriter, msg: Message) -> None:
        frame = msg if isinstance(msg, Frame) else msg.frame
        if frame is None or not frame.needs_reply or writer.is_closing():
            return
        if writer.transport.get_write_buffer_size() > self.max_write_buffer:
            self._counters["replies_dropped"] += 1
            return
        writer.write(encode_result(frame.unit_id, frame.request_id, frame.service_id))
        info.replies += 1
        self._counters["replies_total"] += 1

    async def _read(self, reader: asyncio.StreamReader) -> bytes:
        if self.idle_timeout is None:
            return await reader.read(self.read_size)
        return await asyncio.wait_for(reader.read(self.read_size), self.idle_timeout)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        peername = writer.get_extra_info("peername")
        peer = f"{peername[0]}:{peername[1]}" if isinstance(peername, tuple) else str(peername)
        info = ConnInfo(conn_id=next(self._ids), peer=peer, connected_at=time.time())
        self._conns[info.conn_id] = (info, writer)
        self._counters["connections_total"] += 1
        log.info("NDTP connection #%d from %s", info.conn_id, peer)
        reason = "eof"
        try:
            await self._call(self.on_connect, info)
            while True:
                data = await self._read(reader)
                if not data:
                    break
                info.last_seen = time.time()
                for msg in info.decoder.feed(data):
                    info.messages += 1
                    self._counters["messages_total"] += 1
                    if isinstance(msg, ConnRequest) or info.unit_id is None:
                        info.unit_id = msg.unit_id
                    if self.reply:
                        self._send_reply(info, writer, msg)
                    await self._call(self.on_message, info, msg)
        except TimeoutError:
            reason = "idle timeout"
        except (ConnectionError, OSError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception:
            reason = "handler error"
            log.exception("NDTP connection #%d handler failed", info.conn_id)
        finally:
            self._conns.pop(info.conn_id, None)
            for key in _STAT_KEYS:
                self._closed_stats[key] += info.decoder.stats.get(key, 0)
            writer.close()
            log.info(
                "NDTP connection #%d (%s, unit %s) closed: %s; %d messages, stats=%s",
                info.conn_id,
                peer,
                info.unit_id,
                reason,
                info.messages,
                info.decoder.stats,
            )
            await self._call(self.on_disconnect, info)
