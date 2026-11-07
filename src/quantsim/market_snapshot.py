"""构造具有逐值来源的时点市场快照

validate_projection 校验快照层与字段, 由 MarketDataRepository.snapshot 调用
read_snapshot 查询上市、证券池、行情及披露数据, 只返回指定 as_of 可见的记录
read_snapshot 内的 lineage 创建来源信息, MarketSnapshotRow.field 按名称读取字段
策略和撮合通过 MarketView.snapshot 间接消费这些结果
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

import duckdb

from quantsim.errors import ErrorCode, SimulatorError

RAW_PRICE_FIELDS = frozenset({"open", "high", "low", "close"})
_RAW_FIELDS = RAW_PRICE_FIELDS | {"volume", "tradable"}


@dataclass(frozen=True)
class IntegrityReport:
    expected_sessions: int
    observed_bars: int
    explained_sessions: int
    unexplained_gaps: int
    explicit_statuses: int


@dataclass(frozen=True)
class MarketVersion:
    data_version: str
    source_sha256: str
    source_ref: str
    provider: str
    import_format: str
    integrity: IntegrityReport


@dataclass(frozen=True)
class DataLineage:
    data_version: str
    source_sha256: str
    provider: str
    source_ref: str
    available_at: datetime
    transformations: tuple[str, ...]


@dataclass(frozen=True)
class MarketValue:
    name: str
    value: Decimal | int | bool | None
    lineage: DataLineage | None
    unit: str | None = None
    period_end: date | None = None
    disclosed_at: datetime | None = None


@dataclass(frozen=True)
class MarketSnapshotRow:
    instrument_id: UUID
    symbol: str | None
    symbol_status: Literal["known", "unknown", "ambiguous"]
    status: str
    fields: tuple[MarketValue, ...]
    symbol_lineage: DataLineage | None
    status_lineage: DataLineage | None
    membership_lineage: DataLineage | None

    def field(self, name: str) -> MarketValue:
        for value in self.fields:
            if value.name == name:
                return value
        raise KeyError(name)


@dataclass(frozen=True)
class MarketSnapshot:
    data_version: str
    session_date: date
    as_of: datetime
    pool: str | tuple[UUID, ...]
    layer: Literal["normalized", "research", "simulation"]
    rows: tuple[MarketSnapshotRow, ...]


def validate_projection(fields: tuple[str, ...], layer: str) -> None:
    if layer not in ("normalized", "research"):
        raise SimulatorError(ErrorCode.INVALID_REQUEST, f"不支持的快照层 {layer}")
    if not isinstance(fields, tuple) or any(
        not isinstance(field, str) for field in fields
    ):
        raise SimulatorError(ErrorCode.INVALID_REQUEST, "fields 须为字段名元组")
    if len(set(fields)) != len(fields):
        raise SimulatorError(ErrorCode.INVALID_REQUEST, "快照字段不能重复")
    for field in fields:
        if field in _RAW_FIELDS:
            continue
        if layer == "research" and (
            field == "adjusted_close"
            or field.startswith("fundamental:")
            and field[12:].strip()
        ):
            continue
        raise SimulatorError(
            ErrorCode.INVALID_REQUEST, f"{layer} 快照不支持字段 {field}"
        )


def read_snapshot(
    connection: duckdb.DuckDBPyConnection,
    *,
    version: str,
    manifest: dict,
    day: date,
    as_of: datetime,
    pool: str | tuple[UUID, ...],
    fields: tuple[str, ...],
    layer: Literal["normalized", "research"],
) -> MarketSnapshot:
    def lineage(ref: str, available: datetime, *steps: str) -> DataLineage:
        return DataLineage(
            version,
            manifest["source_sha256"],
            manifest["provider"],
            ref,
            available,
            (manifest["import_format"], *steps),
        )

    membership = {}
    if isinstance(pool, str):
        if (
            connection.execute(
                """
                SELECT 1 FROM market_pool_changes
                WHERE data_version = ? AND pool_name = ? AND available_at <= ?
                LIMIT 1
                """,
                [version, pool, as_of],
            ).fetchone()
            is None
        ):
            raise SimulatorError(ErrorCode.INVALID_REQUEST, f"未知证券池 {pool}")
        changes = connection.execute(
            """
            SELECT instrument_id, is_member, available_at, source_ref
            FROM market_pool_changes
            WHERE data_version = ? AND pool_name = ? AND effective_on <= ? AND available_at <= ?
            QUALIFY row_number() OVER (PARTITION BY instrument_id ORDER BY effective_on DESC) = 1
            ORDER BY instrument_id
            """,
            [version, pool, day, as_of],
        ).fetchall()
        membership = {
            instrument: lineage(ref, available, "point-in-time pool membership")
            for instrument, member, available, ref in changes
            if member
        }
        ids = list(membership)
    elif isinstance(pool, tuple) and all(
        isinstance(instrument, UUID) for instrument in pool
    ):
        ids = list(dict.fromkeys(pool))
    else:
        raise SimulatorError(
            ErrorCode.INVALID_REQUEST, "pool 须为证券池名或证券 UUID 元组"
        )

    if not ids:
        return MarketSnapshot(version, day, as_of, pool, layer, ())

    instruments = connection.execute(
        """
        WITH symbols AS (
            SELECT instrument_id, symbol, available_at, source_ref
            FROM market_symbols
            WHERE data_version = ? AND effective_on <= ? AND available_at <= ?
            QUALIFY row_number() OVER (PARTITION BY instrument_id ORDER BY effective_on DESC) = 1
        ), conflicts AS (
            SELECT s.symbol
            FROM symbols s JOIN market_listings l USING (instrument_id)
            WHERE l.data_version = ? AND l.listed_on <= ? AND l.listing_available_at <= ?
              AND (l.delisted_on IS NULL OR l.delisted_on > ? OR l.delisting_available_at > ?)
            GROUP BY s.symbol HAVING count(*) > 1
        )
        SELECT l.instrument_id, l.listed_on, l.listing_available_at, l.delisted_on,
               l.delisting_available_at, l.source_ref, s.symbol, s.available_at, s.source_ref,
               c.symbol IS NOT NULL
        FROM market_listings l LEFT JOIN symbols s USING (instrument_id)
        LEFT JOIN conflicts c ON s.symbol = c.symbol
        WHERE l.data_version = ? AND l.instrument_id = ANY(?::UUID[])
        """,
        [version, day, as_of, version, day, as_of, day, as_of, version, ids],
    ).fetchall()
    listings = {item[0]: item[1:] for item in instruments}
    if any(instrument not in listings for instrument in ids):
        raise SimulatorError(ErrorCode.INVALID_REQUEST, "指定证券不属于该数据版本")

    # 状态与价格分别过滤，缺行时只展示当时已知的原因
    bars = {
        row[0]: row[1:]
        for row in connection.execute(
            """
            SELECT instrument_id, open, high, low, close, volume, tradable, available_at, source_ref
            FROM market_bars
            WHERE data_version = ? AND session_date = ? AND instrument_id = ANY(?::UUID[])
                AND bar_end_at <= ? AND available_at <= ?
            """,
            [version, day, ids, as_of, as_of],
        ).fetchall()
    }
    statuses = {
        row[0]: row[1:]
        for row in connection.execute(
            """
            SELECT instrument_id, status, available_at, source_ref FROM market_session_status
            WHERE data_version = ? AND session_date = ? AND instrument_id = ANY(?::UUID[])
                AND available_at <= ?
            """,
            [version, day, ids, as_of],
        ).fetchall()
    }
    adjusted = {}
    if "adjusted_close" in fields:
        adjusted = {
            row[0]: row[1:]
            for row in connection.execute(
                """
                SELECT instrument_id, adjusted_close, method, available_at, source_ref
                FROM market_adjusted_prices
                WHERE data_version = ? AND session_date = ? AND instrument_id = ANY(?::UUID[])
                    AND available_at <= ?
                QUALIFY row_number() OVER (PARTITION BY instrument_id ORDER BY available_at DESC) = 1
                """,
                [version, day, ids, as_of],
            ).fetchall()
        }
    metrics = [
        field.removeprefix("fundamental:")
        for field in fields
        if field.startswith("fundamental:")
    ]
    financials = {}
    if metrics:
        financials = {
            (row[0], row[1]): row[2:]
            for row in connection.execute(
                """
                SELECT instrument_id, metric, value, unit, period_end, disclosed_at, available_at, source_ref
                FROM market_fundamentals
                WHERE data_version = ? AND period_end <= ? AND instrument_id = ANY(?::UUID[])
                    AND metric = ANY(?::VARCHAR[]) AND disclosed_at <= ? AND available_at <= ?
                QUALIFY row_number() OVER (
                    PARTITION BY instrument_id, metric
                    ORDER BY period_end DESC, disclosed_at DESC, available_at DESC
                ) = 1
                """,
                [version, day, ids, metrics, as_of, as_of],
            ).fetchall()
        }

    rows = []
    for instrument in ids:
        (
            listed,
            listing_at,
            delisted,
            delisting_at,
            ref,
            symbol,
            symbol_at,
            symbol_ref,
            symbol_conflict,
        ) = listings[instrument]
        bar = bars.get(instrument)
        status_lineage = None
        if listing_at > as_of:
            status = "unavailable"
            symbol = None
            bar = None
        elif day < listed:
            status = "not_listed"
            status_lineage = lineage(ref, listing_at, "listing effective date")
        elif delisted is not None and day >= delisted and delisting_at <= as_of:
            status = "delisted"
            status_lineage = lineage(ref, delisting_at, "delisting effective date")
        elif instrument in statuses:
            status, available, status_ref = statuses[instrument]
            status_lineage = lineage(status_ref, available, "explicit session status")
        elif bar is not None:
            status = "trading" if bar[5] else "non_tradable"
            status_lineage = lineage(bar[7], bar[6], "raw bar tradability")
        else:
            status = "missing"
        bar_values = {}
        bar_lineage = None
        if bar is not None:
            bar_values = dict(
                zip(
                    ("open", "high", "low", "close", "volume", "tradable"),
                    bar[:6],
                    strict=True,
                )
            )
            bar_lineage = lineage(bar[7], bar[6], "raw OHLCV, exact decimal and UTC")

        values = []
        for field in fields:
            if field in bar_values:
                value = MarketValue(field, bar_values[field], bar_lineage)
            elif (
                field == "adjusted_close"
                and instrument in adjusted
                and status != "unavailable"
            ):
                close, method, available, price_ref = adjusted[instrument]
                value = MarketValue(
                    field,
                    close,
                    lineage(price_ref, available, f"provider adjustment: {method}"),
                )
            elif (
                status != "unavailable"
                and (instrument, field.removeprefix("fundamental:")) in financials
                and field.startswith("fundamental:")
            ):
                number, unit, period, disclosed, available, filing_ref = financials[
                    instrument, field.removeprefix("fundamental:")
                ]
                value = MarketValue(
                    field,
                    number,
                    lineage(
                        filing_ref, available, "latest visible period and revision"
                    ),
                    unit=unit,
                    period_end=period,
                    disclosed_at=disclosed,
                )
            elif field == "tradable" and status in (
                "not_listed",
                "delisted",
                "halted",
                "no_trade",
            ):
                value = MarketValue(field, False, status_lineage)
            else:
                value = MarketValue(field, None, None)
            values.append(value)

        symbol_status: Literal["known", "unknown", "ambiguous"] = "unknown"
        symbol_lineage = None
        if symbol is not None:
            if symbol_conflict and status != "delisted":
                symbol_status = "ambiguous"
                symbol_lineage = lineage(
                    symbol_ref, symbol_at, "withheld: conflicting visible symbols"
                )
                symbol = None
            else:
                symbol_status = "known"
                symbol_lineage = lineage(symbol_ref, symbol_at, "point-in-time symbol")
        rows.append(
            MarketSnapshotRow(
                instrument,
                symbol,
                symbol_status,
                status,
                tuple(values),
                symbol_lineage,
                status_lineage,
                membership.get(instrument),
            )
        )
    return MarketSnapshot(version, day, as_of, pool, layer, tuple(rows))
