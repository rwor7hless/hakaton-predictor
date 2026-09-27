"""Движок проигрывания исторического ``traffic.csv`` в NDTP-поток.

Каждый ``unit_id`` получает собственное TCP-соединение (как настоящий
терминал): handshake ``NPH_SGC_CONN_REQUEST``, затем навигационные пакеты.
«Симулированные часы» потока идут со скоростью ``speed`` относительно
реального времени; строки отправляются, когда время потока догоняет их
``event_time``.
"""

from __future__ import annotations

import asyncio
import bisect
import csv
import logging
import math
import os
import time
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any

from ndtp import FrameDecoder, NavCell, encode_conn_request, encode_nav_packet
from ndtp.cells import EPOCH, unix_to_datetime

__all__ = [
    "ReplayData",
    "ReplayEngine",
    "ReplayRecord",
    "ReplaySettings",
    "StreamClock",
    "UnitLink",
    "load_traffic_csv",
    "parse_start_time",
    "record_from_row",
]

log = logging.getLogger("replayer")

STATES = ("idle", "running", "paused", "finished")
MAX_SPEED = 100_000.0


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(slots=True)
class ReplaySettings:
    """Настройки replayer'а.

    Attributes:
        csv_path: Путь к ``traffic.csv``.
        host: Хост NDTP-сервера (backend).
        port: Порт NDTP-сервера.
        speed: Скорость потока по умолчанию (во сколько раз быстрее реального времени).
        start_time: Время начала live-фазы, ``HH:MM`` на дату данных.
        warmup_min: Минут данных до ``start_time``, отправляемых сразу как HISTORY.
        autostart: Стартовать проигрывание при запуске сервиса.
        connect_wait_s: Сколько ждать подключения всех юнитов перед стартом потока.
        handshake_delay_s: Пауза после handshake перед первым пакетом (эмулятор ждёт 200 мс).
        backoff_initial_s: Начальная задержка переподключения.
        backoff_max_s: Максимальная задержка переподключения.
        connect_timeout_s: Таймаут установки TCP-соединения.
    """

    csv_path: str = "/data/validate/traffic.csv"
    host: str = "backend"
    port: int = 9201
    speed: float = 10.0
    start_time: str = "06:30"
    warmup_min: float = 20.0
    autostart: bool = True
    connect_wait_s: float = 10.0
    handshake_delay_s: float = 0.2
    backoff_initial_s: float = 0.5
    backoff_max_s: float = 10.0
    connect_timeout_s: float = 5.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ReplaySettings:
        """Читает настройки из переменных окружения.

        Поддерживаются ``TRAFFIC_CSV``, ``NDTP_HOST``, ``NDTP_PORT``,
        ``REPLAY_SPEED``, ``REPLAY_START``, ``REPLAY_WARMUP_MIN``,
        ``REPLAY_AUTOSTART``, ``REPLAY_CONNECT_WAIT_S``.

        Args:
            env: Источник переменных; по умолчанию ``os.environ``.

        Returns:
            Заполненные настройки.
        """
        e = os.environ if env is None else env
        d = cls()
        return cls(
            csv_path=e.get("TRAFFIC_CSV", d.csv_path),
            host=e.get("NDTP_HOST", d.host),
            port=int(e.get("NDTP_PORT", d.port)),
            speed=float(e.get("REPLAY_SPEED", d.speed)),
            start_time=e.get("REPLAY_START", d.start_time),
            warmup_min=float(e.get("REPLAY_WARMUP_MIN", d.warmup_min)),
            autostart=_env_bool(e.get("REPLAY_AUTOSTART"), d.autostart),
            connect_wait_s=float(e.get("REPLAY_CONNECT_WAIT_S", d.connect_wait_s)),
        )


@dataclass(slots=True)
class ReplayRecord:
    """Одна строка телеметрии, готовая к отправке.

    Attributes:
        ts: ``event_time`` в unix-секундах с дробной частью (для сортировки и часов).
        unit_id: ID терминала.
        nav: Навигационная ячейка.
        history: Строка помечена ``is_hist_data`` и уходит как HISTORY.
    """

    ts: float
    unit_id: int
    nav: NavCell
    history: bool


@dataclass(slots=True)
class ReplayData:
    """Загруженный и отсортированный по ``event_time`` CSV.

    Attributes:
        records: Записи в порядке ``event_time``.
        times: ``ts`` записей (для бинарного поиска).
        units: Отсортированный список ``unit_id``.
        day: Дата первой записи — к ней привязывается ``start_time``.
        source: Путь к файлу.
    """

    records: list[ReplayRecord]
    times: list[float]
    units: list[int]
    day: date
    source: str


def _num(value: str | None) -> float:
    if value is None:
        return math.nan
    value = value.strip()
    if not value:
        return math.nan
    try:
        return float(value)
    except ValueError:
        return math.nan


def _u16(value: float) -> int:
    if math.isnan(value) or value <= 0:
        return 0
    return min(round(value), 0xFFFF)


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "1", "yes")


def record_from_row(row: Mapping[str, str | None]) -> ReplayRecord | None:
    """Преобразует строку ``traffic.csv`` в запись для отправки.

    Правила: время ``event_time`` трактуется как наивное UTC;
    ``location_valid=False`` (или пустые координаты) → ``valid=False`` и нулевые
    координаты; NaN-скорость → 0; ``alt`` и ``speed`` клипаются в 0…65535;
    ``heading`` → ``course``.

    Args:
        row: Словарь колонок CSV.

    Returns:
        Запись или ``None``, если нет ``unit_id``/``event_time``.
    """
    try:
        event_time = datetime.fromisoformat((row.get("event_time") or "").strip())
        unit_id = int((row.get("unit_id") or "").strip())
    except ValueError:
        return None
    ts = (event_time - EPOCH).total_seconds()
    lon = _num(row.get("lon"))
    lat = _num(row.get("lat"))
    valid = _is_true(row.get("location_valid")) and not (math.isnan(lon) or math.isnan(lat))
    if not valid:
        lon = lat = 0.0
    speed = _u16(_num(row.get("speed")))
    nav = NavCell(
        timestamp=int(ts),
        lon=lon,
        lat=lat,
        valid=valid,
        speed_avg=speed,
        speed_max=speed,
        course=_u16(_num(row.get("heading"))),
        altitude=_u16(_num(row.get("alt"))),
    )
    return ReplayRecord(ts=ts, unit_id=unit_id, nav=nav, history=_is_true(row.get("is_hist_data")))


def load_traffic_csv(path: str | Path) -> ReplayData:
    """Читает ``traffic.csv`` и сортирует строки по ``event_time`` (стабильно).

    Args:
        path: Путь к CSV.

    Returns:
        Данные для проигрывания.

    Raises:
        FileNotFoundError: Если файла нет.
        ValueError: Если в файле нет ни одной корректной строки.
    """
    records: list[ReplayRecord] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rec = record_from_row(row)
            if rec is not None:
                records.append(rec)
    if not records:
        raise ValueError(f"no usable rows in {path}")
    records.sort(key=lambda r: r.ts)
    times = [r.ts for r in records]
    return ReplayData(
        records=records,
        times=times,
        units=sorted({r.unit_id for r in records}),
        day=unix_to_datetime(times[0]).date(),
        source=str(path),
    )


def parse_start_time(value: str, day: date) -> datetime:
    """Разбирает время старта потока.

    Args:
        value: ``HH:MM``, ``HH:MM:SS`` или полная дата-время ISO.
        day: Дата данных, к которой привязывается время без даты.

    Returns:
        Наивное UTC-время начала live-фазы.

    Raises:
        ValueError: Если формат не распознан.
    """
    value = value.strip()
    try:
        return datetime.combine(day, dtime.fromisoformat(value))
    except ValueError:
        pass
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=None) if dt.tzinfo is None else dt.astimezone().replace(tzinfo=None)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return unix_to_datetime(math.floor(ts)).isoformat(timespec="seconds")


def _speed_out(speed: float) -> float | int:
    return int(speed) if float(speed).is_integer() else speed


class StreamClock:
    """Часы потока: ``stream = base + (monotonic - wall_base) * speed`` с паузой.

    Args:
        speed: Во сколько раз поток быстрее реального времени.
    """

    def __init__(self, speed: float) -> None:
        self.speed = speed
        self._base = 0.0
        self._wall = time.monotonic()
        self._frozen: float | None = None

    def anchor(self, stream_ts: float) -> None:
        """Ставит текущее время потока, сохраняя состояние паузы.

        Args:
            stream_ts: Время потока, unix-секунды.
        """
        if self._frozen is not None:
            self._frozen = stream_ts
        self._base = stream_ts
        self._wall = time.monotonic()

    def now(self) -> float:
        """Текущее время потока, unix-секунды."""
        if self._frozen is not None:
            return self._frozen
        return self._base + (time.monotonic() - self._wall) * self.speed

    @property
    def paused(self) -> bool:
        """Остановлены ли часы."""
        return self._frozen is not None

    def pause(self) -> None:
        """Замораживает часы."""
        if self._frozen is None:
            self._frozen = self.now()

    def resume(self) -> None:
        """Продолжает ход часов с замороженного момента."""
        if self._frozen is not None:
            frozen, self._frozen = self._frozen, None
            self.anchor(frozen)

    def set_speed(self, speed: float) -> None:
        """Меняет скорость без скачка времени потока.

        Args:
            speed: Новая скорость.
        """
        if self._frozen is None:
            self.anchor(self.now())
        self.speed = speed


class UnitLink:
    """TCP-соединение одного терминала с переподключением.

    Пакеты ставятся в очередь :meth:`enqueue` и отправляются фоновой задачей
    :meth:`run`. Ответы сервера вычитываются и считаются (``acks``).

    Args:
        unit_id: ID терминала.
        host: Хост NDTP-сервера.
        port: Порт NDTP-сервера.
        handshake_delay: Пауза после handshake перед первым пакетом.
        backoff_initial: Начальная задержка переподключения.
        backoff_max: Максимальная задержка переподключения.
        connect_timeout: Таймаут подключения.

    Attributes:
        connected: Соединение установлено и handshake отправлен.
        sent: Отправлено навигационных пакетов.
        acks: Получено ``NPH_RESULT``.
        connections: Успешных подключений.
        connect_failures: Неудачных попыток подключения.
        queue: Очередь ``(nav, history)`` на отправку.
    """

    def __init__(
        self,
        unit_id: int,
        host: str,
        port: int,
        *,
        handshake_delay: float = 0.2,
        backoff_initial: float = 0.5,
        backoff_max: float = 10.0,
        connect_timeout: float = 5.0,
    ) -> None:
        self.unit_id = unit_id
        self.host = host
        self.port = port
        self.handshake_delay = handshake_delay
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.connect_timeout = connect_timeout
        self.connected = False
        self.sent = 0
        self.acks = 0
        self.connections = 0
        self.connect_failures = 0
        self.queue: deque[tuple[NavCell, bool]] = deque()
        self._wake = asyncio.Event()
        self._request_id = 0

    def enqueue(self, nav: NavCell, history: bool) -> None:
        """Ставит навигационную точку в очередь отправки.

        Args:
            nav: Навигационная ячейка.
            history: Отправить как ``NPH_SND_HISTORY``.
        """
        self.queue.append((nav, history))
        self._wake.set()

    def _next_request_id(self) -> int:
        self._request_id = self._request_id % 0xFFFFFFFF + 1
        return self._request_id

    async def run(self) -> None:
        """Цикл «подключиться → handshake → слать очередь», с backoff при ошибках."""
        backoff = self.backoff_initial
        while True:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), self.connect_timeout
                )
            except (OSError, TimeoutError) as exc:
                self.connect_failures += 1
                if self.connect_failures == 1 or self.connect_failures % 30 == 0:
                    log.warning("unit %s: connect to %s:%s failed (%s)", self.unit_id, self.host, self.port, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.backoff_max)
                continue
            backoff = self.backoff_initial
            self.connections += 1
            reader_task = asyncio.create_task(self._read_loop(reader))
            try:
                await self._session(writer, reader_task)
            except OSError as exc:
                log.info("unit %s: connection lost (%s), reconnecting", self.unit_id, exc)
            except Exception:
                log.exception("unit %s: session failed, reconnecting", self.unit_id)
            finally:
                self.connected = False
                reader_task.cancel()
                writer.close()
                with suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), 1.0)
            await asyncio.sleep(backoff)

    async def _session(self, writer: asyncio.StreamWriter, reader_task: asyncio.Task[None]) -> None:
        self._request_id = 0
        writer.write(encode_conn_request(self.unit_id, self._next_request_id()))
        await writer.drain()
        if self.handshake_delay > 0:
            await asyncio.sleep(self.handshake_delay)
        self.connected = True
        while True:
            if reader_task.done():
                raise ConnectionResetError("closed by server")
            if not self.queue:
                self._wake.clear()
                waiter = asyncio.create_task(self._wake.wait())
                try:
                    await asyncio.wait({waiter, reader_task}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiter.cancel()
                continue
            batch = 0
            while self.queue and batch < 256:
                nav, history = self.queue[0]
                writer.write(encode_nav_packet(self.unit_id, self._next_request_id(), nav, history=history))
                self.queue.popleft()
                batch += 1
            await writer.drain()
            self.sent += batch

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        decoder = FrameDecoder()
        with suppress(OSError):
            while data := await reader.read(65536):
                for msg in decoder.feed(data):
                    if getattr(msg, "result_code", None) is not None:
                        self.acks += 1


class ReplayEngine:
    """Проигрывает CSV в NDTP: прогрев как HISTORY, затем live по часам потока.

    Состояния: ``idle`` → ``running`` ⇄ ``paused`` → ``finished``; ``stop``
    возвращает в ``idle`` и закрывает соединения.

    Args:
        settings: Настройки (путь к CSV, адрес backend, параметры по умолчанию).
    """

    def __init__(self, settings: ReplaySettings) -> None:
        self.settings = settings
        self.state = "idle"
        self.phase: str | None = None
        self.speed = settings.speed
        self.start_time = settings.start_time
        self.warmup_min = settings.warmup_min
        self.error: str | None = None
        self.dispatched = 0
        self.total = 0
        self._data: ReplayData | None = None
        self._links: dict[int, UnitLink] = {}
        self._link_tasks: list[asyncio.Task[None]] = []
        self._task: asyncio.Task[None] | None = None
        self._clock = StreamClock(self.speed)
        self._stream_ts: float | None = None
        self._stopped_at: float | None = None
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._wakeup = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def links(self) -> dict[int, UnitLink]:
        """Соединения по ``unit_id``."""
        return self._links

    async def load(self) -> ReplayData:
        """Загружает CSV (один раз, в отдельном потоке).

        Returns:
            Загруженные данные.
        """
        if self._data is None:
            t0 = time.monotonic()
            self._data = await asyncio.to_thread(load_traffic_csv, self.settings.csv_path)
            log.info(
                "loaded %d rows, %d units from %s in %.1fs",
                len(self._data.records),
                len(self._data.units),
                self._data.source,
                time.monotonic() - t0,
            )
        return self._data

    async def start(
        self,
        speed: float | None = None,
        start_time: str | None = None,
        warmup_min: float | None = None,
    ) -> dict[str, Any]:
        """Запускает проигрывание с начала (текущее, если есть, останавливается).

        Args:
            speed: Скорость потока; ``None`` — последняя заданная.
            start_time: Начало live-фазы (``HH:MM``); ``None`` — последнее заданное.
            warmup_min: Длительность прогрева в минутах; ``None`` — последняя заданная.

        Returns:
            Статус после запуска.

        Raises:
            ValueError: Некорректные параметры.
            FileNotFoundError: Нет CSV.
        """
        async with self._lock:
            speed = self.speed if speed is None else float(speed)
            warmup = self.warmup_min if warmup_min is None else float(warmup_min)
            if not 0 < speed <= MAX_SPEED:
                raise ValueError(f"speed must be in (0, {MAX_SPEED:g}]")
            if warmup < 0:
                raise ValueError("warmup_min must be >= 0")
            data = await self.load()
            start_dt = parse_start_time(start_time or self.start_time, data.day)
            await self._shutdown()
            self.speed, self.warmup_min = speed, warmup
            self.start_time = start_dt.strftime("%H:%M" if start_dt.second == 0 else "%H:%M:%S")
            start_ts = (start_dt - EPOCH).total_seconds()
            warm_ts = start_ts - warmup * 60.0
            first = bisect.bisect_left(data.times, warm_ts)
            self.total = len(data.records) - first
            self.dispatched = 0
            self.error = None
            self._stream_ts = warm_ts
            self._clock = StreamClock(speed)
            self._resumed.set()
            self._wakeup.clear()
            s = self.settings
            self._links = {
                u: UnitLink(
                    u,
                    s.host,
                    s.port,
                    handshake_delay=s.handshake_delay_s,
                    backoff_initial=s.backoff_initial_s,
                    backoff_max=s.backoff_max_s,
                    connect_timeout=s.connect_timeout_s,
                )
                for u in data.units
            }
            self._link_tasks = [asyncio.create_task(link.run(), name=f"unit-{u}") for u, link in self._links.items()]
            self.state = "running"
            self.phase = "connecting"
            self._task = asyncio.create_task(self._run(data, first, start_ts), name="replay")
            log.info(
                "replay started: %s speed=%g warmup=%gmin, %d rows, %d units -> %s:%s",
                start_dt.isoformat(),
                speed,
                warmup,
                self.total,
                len(self._links),
                s.host,
                s.port,
            )
            return self.status()

    async def stop(self) -> dict[str, Any]:
        """Останавливает проигрывание и закрывает все соединения.

        Returns:
            Статус после остановки.
        """
        async with self._lock:
            await self._shutdown()
            return self.status()

    def pause(self) -> dict[str, Any]:
        """Ставит поток на паузу (соединения остаются открытыми).

        Returns:
            Текущий статус.
        """
        if self.state == "running":
            self.state = "paused"
            self._clock.pause()
            self._resumed.clear()
            self._wakeup.set()
        return self.status()

    def resume(self) -> dict[str, Any]:
        """Продолжает поток после паузы.

        Returns:
            Текущий статус.
        """
        if self.state == "paused":
            self.state = "running"
            self._clock.resume()
            self._resumed.set()
            self._wakeup.set()
        return self.status()

    def set_speed(self, speed: float) -> dict[str, Any]:
        """Меняет скорость на лету без скачка времени потока.

        Args:
            speed: Новая скорость, ``(0, 100000]``.

        Returns:
            Текущий статус.

        Raises:
            ValueError: Скорость вне диапазона.
        """
        speed = float(speed)
        if not 0 < speed <= MAX_SPEED:
            raise ValueError(f"speed must be in (0, {MAX_SPEED:g}]")
        self.speed = speed
        self._clock.set_speed(speed)
        self._wakeup.set()
        return self.status()

    async def wait_finished(self, timeout: float | None = None) -> bool:
        """Ждёт окончания данных.

        Args:
            timeout: Предельное время ожидания, секунды.

        Returns:
            ``True``, если проигрывание дошло до ``finished``.
        """
        task = self._task
        if task is None:
            return self.state == "finished"
        await asyncio.wait({task}, timeout=timeout)
        return self.state == "finished"

    def stream_time(self) -> float | None:
        """Текущее время потока, unix-секунды, или ``None`` до первого старта."""
        if self.state == "idle":
            return self._stopped_at
        if self.phase in ("live", "done"):
            return self._clock.now()
        return self._stream_ts

    def status(self) -> dict[str, Any]:
        """Снимок состояния для ``GET /api/status``.

        Returns:
            Словарь с ключами контракта (``state``, ``speed``, ``stream_time``,
            ``start_time``, ``units``, ``sent``, ``connected``) и служебными полями.
        """
        links = self._links.values()
        return {
            "state": self.state,
            "speed": _speed_out(self.speed),
            "stream_time": _iso(self.stream_time()),
            "start_time": self.start_time,
            "warmup_min": _speed_out(self.warmup_min),
            "units": len(self._links) if self._links else (len(self._data.units) if self._data else 0),
            "sent": sum(link.sent for link in links),
            "connected": sum(1 for link in links if link.connected),
            "phase": self.phase,
            "dispatched": self.dispatched,
            "total": self.total,
            "queued": sum(len(link.queue) for link in links),
            "acks": sum(link.acks for link in links),
            "reconnects": sum(max(link.connections - 1, 0) for link in links),
            "target": f"{self.settings.host}:{self.settings.port}",
            "error": self.error,
        }

    async def _shutdown(self) -> None:
        task, self._task = self._task, None
        tasks = ([task] if task is not None else []) + self._link_tasks
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._link_tasks = []
        for link in self._links.values():
            link.connected = False
        if self.state != "idle":
            self._stopped_at = self.stream_time()
            log.info("replay stopped at %s", _iso(self._stopped_at))
        self.state = "idle"
        self.phase = None

    def _dispatch(self, rec: ReplayRecord, history: bool) -> None:
        link = self._links[rec.unit_id]
        link.enqueue(rec.nav, history or not link.connected)
        self.dispatched += 1

    async def _wait_connections(self) -> None:
        deadline = time.monotonic() + self.settings.connect_wait_s
        while time.monotonic() < deadline:
            if all(link.connected for link in self._links.values()):
                return
            await asyncio.sleep(0.05)
        missing = sum(1 for link in self._links.values() if not link.connected)
        if missing:
            log.warning("%d/%d units not connected yet, starting anyway", missing, len(self._links))

    async def _sleep_until_wakeup(self, seconds: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._wakeup.wait(), seconds)
        self._wakeup.clear()

    async def _run(self, data: ReplayData, i: int, start_ts: float) -> None:
        records, times, n = data.records, data.times, len(data.records)
        try:
            await self._wait_connections()
            self.phase = "warmup"
            live_from = bisect.bisect_left(times, start_ts)
            while i < live_from:
                await self._resumed.wait()
                end = min(i + 500, live_from)
                for k in range(i, end):
                    self._dispatch(records[k], True)
                self._stream_ts = times[end - 1]
                i = end
                await asyncio.sleep(0)
            self._clock.anchor(start_ts)
            self.phase = "live"
            while i < n:
                await self._resumed.wait()
                now = self._clock.now()
                while i < n and times[i] <= now:
                    rec = records[i]
                    self._dispatch(rec, rec.history)
                    i += 1
                if i < n:
                    await self._sleep_until_wakeup(min(max((times[i] - now) / self._clock.speed, 0.001), 0.25))
            self._clock.anchor(max(self._clock.now(), times[-1]))
            self._clock.pause()
            self.phase = "done"
            self.state = "finished"
            log.info("replay finished: %d rows dispatched", self.dispatched)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "finished"
            log.exception("replay failed")
