"""导入冻结的日频行情，并按可用时点读取观察记录。"""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import duckdb

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.market_bundle import (
    decimal_value,
    parse_metadata,
    write_metadata,
)
from quantsim.market_bundle import (
    text as _text,
)
from quantsim.market_bundle import (
    timestamp as _timestamp,
)
from quantsim.market_snapshot import (
    IntegrityReport,
    MarketSnapshot,
    MarketVersion,
    read_snapshot,
    validate_projection,
)


@dataclass(frozen=True)
class Session:
    session_date: date
    market_close_at: datetime
    cutoff_at: datetime


@dataclass(frozen=True)
class Tick(Session):
    seq: int


@dataclass(frozen=True)
class DatasetRef:
    data_version: str


@dataclass(frozen=True)
class Bar:
    instrument_id: UUID
    ticker: str
    session_date: date
    bar_end_at: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    tradable: bool
    source_ref: str


@dataclass(frozen=True)
class BarBatch:
    data_version: str
    as_of: datetime
    bars: tuple[Bar, ...]


@dataclass(frozen=True)
class MarketView:
    """以固定交易日和发布时间提供历史查询。"""

    _repository: "MarketDataRepository"
    data_version: str
    _sessions: tuple[Session, ...]

    @property
    def session(self) -> Session:
        return self._sessions[-1]

    def history(
        self, instruments: tuple[UUID, ...], *, lookback_sessions: int
    ) -> BarBatch:
        if type(lookback_sessions) is not int or lookback_sessions < 1:
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST, "lookback_sessions 须为正整数"
            )
        return self._repository.history(
            self.data_version,
            instruments,
            start=self._sessions[
                -min(lookback_sessions, len(self._sessions))
            ].session_date,
            end=self.session.session_date,
            as_of=self.session.cutoff_at,
        )

    def snapshot(
        self,
        *,
        pool: str | tuple[UUID, ...],
        fields: tuple[str, ...],
        layer: Literal["normalized", "research"] = "normalized",
    ) -> MarketSnapshot:
        """按绑定交易日的 cutoff 读取快照，供策略使用。"""
        return self._repository.snapshot(
            self.data_version,
            self.session.session_date,
            pool=pool,
            fields=fields,
            layer=layer,
        )


class MarketDataRepository:
    """对同一 DuckDB 文件串行导入和读取，读取连接始终只读。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def import_batch(self, source: Path) -> DatasetRef:
        """接入冻结 JSON 批次，原子写入清单和行情。"""
        try:
            raw = source.read_bytes()
        except OSError as exc:
            raise SimulatorError(
                ErrorCode.DATA_UNAVAILABLE, f"无法读取行情批次 {source}"
            ) from exc

        try:
            manifest = json.loads(raw)
            format_version = manifest.get("format_version")
            if type(format_version) is not int or format_version != 2:
                raise ValueError("行情批次须使用 format_version: 2 的历史市场格式")
            import_format = "market-json-v2"
            version = sha256(import_format.encode() + b"\0" + raw).hexdigest()
            bars = manifest.pop("bars")
            if not all(
                isinstance(rows, list)
                for rows in (bars, manifest["sessions"], manifest["instruments"])
            ):
                raise TypeError("bars、sessions 和 instruments 须为 JSON 数组")
            manifest["source_sha256"] = sha256(raw).hexdigest()
            manifest["import_format"] = import_format
            instruments, rows, closes = _parse_batch(manifest, bars)
            metadata = parse_metadata(manifest, bars, closes)
        except (
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
            InvalidOperation,
            OverflowError,
        ) as exc:
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST, f"行情批次格式无效: {source} ({exc})"
            ) from exc

        try:
            with duckdb.connect(self._path) as connection:
                connection.begin()
                existing = connection.execute(
                    "SELECT 1 FROM market_datasets WHERE data_version = ?", [version]
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO market_datasets (data_version, manifest, payload) VALUES (?, ?, ?)",
                        [version, json.dumps(manifest, ensure_ascii=False), raw],
                    )
                    for instrument in instruments:
                        _insert_instrument(connection, instrument)
                    write_metadata(connection, version, metadata)
                    if rows:
                        _insert_bars(connection, version, rows)
                connection.commit()
        except duckdb.ConstraintException as exc:
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST, f"行情批次违反数据库约束: {source}"
            ) from exc
        except duckdb.Error as exc:
            raise SimulatorError(
                ErrorCode.DATA_UNAVAILABLE, "无法导入行情批次"
            ) from exc
        return DatasetRef(version)

    def version(self, data_version: str) -> MarketVersion:
        """读取冻结批次的来源、格式和导入完整性摘要。"""
        try:
            with duckdb.connect(self._path, read_only=True) as connection:
                manifest = _read_manifest(connection, data_version)
        except duckdb.Error as exc:
            raise SimulatorError(
                ErrorCode.DATA_UNAVAILABLE, "无法读取行情版本"
            ) from exc
        return MarketVersion(
            data_version,
            manifest["source_sha256"],
            manifest["source_ref"],
            manifest["provider"],
            manifest["import_format"],
            IntegrityReport(**manifest["integrity"]),
        )

    def versions(self) -> tuple[str, ...]:
        """列出已导入批次 ID，供调用方显式锁定版本。"""
        try:
            with duckdb.connect(self._path, read_only=True) as connection:
                return tuple(
                    version
                    for (version,) in connection.execute(
                        "SELECT data_version FROM market_datasets ORDER BY imported_at, data_version"
                    ).fetchall()
                )
        except duckdb.Error as exc:
            raise SimulatorError(
                ErrorCode.DATA_UNAVAILABLE, "无法列出行情版本"
            ) from exc

    def pools(self, data_version: str) -> tuple[str, ...]:
        """列出冻结批次内可查询的证券池名称。"""
        try:
            with duckdb.connect(self._path, read_only=True) as connection:
                _read_manifest(connection, data_version)
                rows = connection.execute(
                    "SELECT DISTINCT pool_name FROM market_pool_changes "
                    "WHERE data_version = ? ORDER BY pool_name",
                    [data_version],
                ).fetchall()
        except duckdb.Error as exc:
            raise SimulatorError(ErrorCode.DATA_UNAVAILABLE, "无法列出证券池") from exc
        return tuple(name for (name,) in rows)

    def raw_source(self, data_version: str) -> bytes:
        """返回导入时归档的精确字节，并核对清单摘要。"""
        try:
            with duckdb.connect(self._path, read_only=True) as connection:
                row = connection.execute(
                    "SELECT manifest, payload FROM market_datasets WHERE data_version = ?",
                    [data_version],
                ).fetchone()
        except duckdb.Error as exc:
            raise SimulatorError(
                ErrorCode.DATA_UNAVAILABLE, "无法读取原始行情归档"
            ) from exc
        if row is None:
            raise SimulatorError(
                ErrorCode.DATA_UNAVAILABLE, f"未知行情版本 {data_version}"
            )
        manifest_json, raw = row
        manifest = json.loads(manifest_json)
        if (
            sha256(raw).hexdigest() != manifest["source_sha256"]
            or sha256(manifest["import_format"].encode() + b"\0" + raw).hexdigest()
            != data_version
        ):
            raise SimulatorError(ErrorCode.INVALID_LINEAGE, "原始行情归档摘要不符")
        return raw

    def snapshot(
        self,
        data_version: str,
        session_date: date,
        *,
        pool: str | tuple[UUID, ...],
        fields: tuple[str, ...],
        layer: Literal["normalized", "research"] = "normalized",
        known_on: date | None = None,
    ) -> MarketSnapshot:
        """按版本、交易日、证券池和字段读取历史快照。"""
        validate_projection(fields, layer)
        if known_on is not None and (layer != "research" or known_on < session_date):
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST, "known_on 只接受研究层的当日或后续交易日"
            )
        try:
            with duckdb.connect(self._path, read_only=True) as connection:
                manifest = _read_manifest(connection, data_version)
                calendar = {
                    session.session_date: session.cutoff_at
                    for session in _sessions(manifest)
                }
                if session_date not in calendar or (
                    known_on is not None and known_on not in calendar
                ):
                    raise SimulatorError(
                        ErrorCode.INVALID_REQUEST, "快照日期须在批次交易日历内"
                    )
                connection.execute("SET TimeZone = 'UTC'")
                return read_snapshot(
                    connection,
                    version=data_version,
                    manifest=manifest,
                    day=session_date,
                    as_of=calendar[known_on or session_date],
                    pool=pool,
                    fields=fields,
                    layer=layer,
                )
        except duckdb.Error as exc:
            raise SimulatorError(
                ErrorCode.DATA_UNAVAILABLE, "无法读取历史快照"
            ) from exc

    def sessions(
        self, data_version: str, start: date, end: date
    ) -> tuple[Session, ...]:
        _check_range(start, end)
        try:
            with duckdb.connect(self._path, read_only=True) as connection:
                manifest = _read_manifest(connection, data_version)
        except duckdb.Error as exc:
            raise SimulatorError(
                ErrorCode.DATA_UNAVAILABLE, "无法读取交易日历"
            ) from exc
        return tuple(
            session
            for session in _sessions(manifest)
            if start <= session.session_date <= end
        )

    def view(self, data_version: str, session_date: date) -> MarketView:
        sessions = self.sessions(data_version, date.min, session_date)
        if not sessions or sessions[-1].session_date != session_date:
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST, f"{session_date} 不是批次交易日"
            )
        return MarketView(self, data_version, sessions)

    def history(
        self,
        data_version: str,
        instruments: tuple[UUID, ...],
        *,
        start: date,
        end: date,
        as_of: datetime,
    ) -> BarBatch:
        _check_range(start, end)
        if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "as_of 须带时区")
        as_of = as_of.astimezone(UTC)
        try:
            with duckdb.connect(self._path, read_only=True) as connection:
                _read_manifest(connection, data_version)
                connection.execute("SET TimeZone = 'UTC'")
                rows = connection.execute(
                    """
                    SELECT b.instrument_id, i.instrument_key, b.session_date,
                           b.bar_end_at, b.available_at, b.open, b.high, b.low, b.close,
                           b.volume, b.tradable, b.source_ref
                    FROM market_bars AS b JOIN instruments AS i USING (instrument_id)
                    JOIN market_listings AS l
                        ON l.data_version = b.data_version AND l.instrument_id = b.instrument_id
                    WHERE b.data_version = ?
                      AND b.instrument_id = ANY(?::UUID[])
                      AND b.session_date BETWEEN ? AND ?
                      AND b.bar_end_at <= ? AND b.available_at <= ?
                      AND l.listing_available_at <= ?
                    ORDER BY b.session_date, i.instrument_key
                    """,
                    [data_version, list(instruments), start, end, as_of, as_of, as_of],
                ).fetchall()
        except duckdb.Error as exc:
            raise SimulatorError(ErrorCode.DATA_UNAVAILABLE, "无法读取行情") from exc
        return BarBatch(
            data_version,
            as_of,
            tuple(Bar(*row) for row in rows),
        )

    def session_bars(
        self, data_version: str, instruments: tuple[UUID, ...], *, tick: Tick
    ) -> BarBatch:
        return self.history(
            data_version,
            instruments,
            start=tick.session_date,
            end=tick.session_date,
            as_of=tick.cutoff_at,
        )


def _check_range(start: date, end: date) -> None:
    if start > end:
        raise SimulatorError(ErrorCode.INVALID_REQUEST, "开始日期晚于结束日期")


def _read_manifest(connection: duckdb.DuckDBPyConnection, version: str) -> dict:
    row = connection.execute(
        "SELECT manifest FROM market_datasets WHERE data_version = ?", [version]
    ).fetchone()
    if row is None:
        raise SimulatorError(ErrorCode.DATA_UNAVAILABLE, f"找不到行情批次 {version}")
    return json.loads(row[0])


def _sessions(manifest: dict) -> tuple[Session, ...]:
    return tuple(
        Session(
            date.fromisoformat(item["session_date"]),
            _timestamp(item["market_close_at"]),
            _timestamp(item["cutoff_at"]),
        )
        for item in manifest["sessions"]
    )


def _price(value: str) -> Decimal:
    return decimal_value(value, precision=20, positive=True)


def _parse_batch(
    manifest: dict, bars: list
) -> tuple[list[tuple], list[tuple], dict[date, datetime]]:
    venue = _text(manifest, "venue")
    currency = _text(manifest, "currency")
    timezone_name = _text(manifest, "exchange_timezone")
    source_ref = _text(manifest, "source_ref")
    zone = ZoneInfo(timezone_name)
    sessions = _sessions(manifest)
    if not sessions or any(
        session.cutoff_at < session.market_close_at
        or session.market_close_at.astimezone(zone).date() != session.session_date
        for session in sessions
    ):
        raise ValueError("交易日历为空或收盘时点与交易日、cutoff 不匹配")
    if any(
        session.cutoff_at.astimezone(zone).date() != session.session_date
        for session in sessions
    ):
        raise ValueError("历史市场 cutoff 须在交易所当地同一交易日")
    if any(
        left.session_date >= right.session_date
        or left.market_close_at >= right.market_close_at
        or left.cutoff_at >= right.cutoff_at
        for left, right in pairwise(sessions)
    ):
        raise ValueError("交易日历须严格递增")
    close_by_date = {
        session.session_date: session.market_close_at for session in sessions
    }

    instruments = [
        (
            UUID(item["instrument_id"]),
            venue,
            _text(item, "symbol"),
            _text(item, "instrument_key"),
            _text(item, "name"),
            currency,
            timezone_name,
            source_ref,
        )
        for item in manifest["instruments"]
    ]
    instrument_ids = {instrument[3]: instrument[0] for instrument in instruments}
    if (
        not instruments
        or len(instrument_ids) != len(instruments)
        or (len(set(instrument_ids.values())) != len(instruments))
    ):
        raise ValueError("证券表不能为空，证券 ID 与 instrument_key 须唯一")

    rows = []
    for index, bar in enumerate(bars):
        session_date = date.fromisoformat(bar["session_date"])
        volume, tradable = bar["volume"], bar["tradable"]
        if (
            type(volume) is not int
            or not 0 <= volume < 2**63
            or type(tradable) is not bool
        ):
            raise ValueError("成交量须为非负整数，tradable 须为布尔值")
        rows.append(
            (
                instrument_ids[bar["instrument_key"]],
                session_date,
                close_by_date[session_date],
                _timestamp(bar["available_at"]),
                *(_price(bar[key]) for key in ("open", "high", "low", "close")),
                volume,
                tradable,
                f"{source_ref}#bars/{index}",
            )
        )
    rows.sort(key=lambda row: (row[0], row[1]))
    return instruments, rows, close_by_date


def _insert_instrument(
    connection: duckdb.DuckDBPyConnection, instrument: tuple
) -> None:
    connection.execute(
        """
        INSERT INTO instruments (
            instrument_id, venue, symbol, instrument_key, name, currency, exchange_timezone, source_ref
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (instrument_id) DO NOTHING
        """,
        instrument,
    )
    instrument_id, venue, _, key, _, currency, timezone, _ = instrument
    # 名称和代码保存在各批次中，跨版本只核对稳定身份字段。
    saved = connection.execute(
        """
        SELECT venue, instrument_key, currency, exchange_timezone
        FROM instruments WHERE instrument_id = ?
        """,
        [instrument_id],
    ).fetchone()
    if saved != (venue, key, currency, timezone):
        raise SimulatorError(
            ErrorCode.INVALID_LINEAGE, f"证券身份发生变更: {instrument_id}"
        )


def _insert_bars(
    connection: duckdb.DuckDBPyConnection, version: str, rows: list[tuple]
) -> None:
    # 每批用列数组送入 DuckDB，避免逐行情执行一条 INSERT
    batch_size = 5_000
    for start in range(0, len(rows), batch_size):
        columns = [
            list(column)
            for column in zip(*rows[start : start + batch_size], strict=True)
        ]
        connection.execute(
            """
            INSERT INTO market_bars (
                data_version, instrument_id, session_date, bar_end_at, available_at,
                open, high, low, close, volume, tradable, source_ref
            )
            SELECT ?, unnest(?::UUID[]), unnest(?::DATE[]), unnest(?::TIMESTAMPTZ[]),
                   unnest(?::TIMESTAMPTZ[]), unnest(?::DECIMAL(20,8)[]),
                   unnest(?::DECIMAL(20,8)[]), unnest(?::DECIMAL(20,8)[]),
                   unnest(?::DECIMAL(20,8)[]), unnest(?::BIGINT[]), unnest(?::BOOLEAN[]),
                   unnest(?::VARCHAR[])
            """,
            [version, *columns],
        )
