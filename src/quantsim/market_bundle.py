"""校验并规范化历史市场附属数据。"""

import re
from bisect import bisect_left
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Context, Decimal
from itertools import pairwise
from uuid import UUID
from zoneinfo import ZoneInfo

import duckdb


def timestamp(value: str) -> datetime:
    if any(fraction[6:].strip("0") for fraction in re.findall(r"[.,](\d+)", value)):
        raise ValueError("行情时刻精度超出数据库可保存的微秒")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("行情时刻须带时区")
    # fromisoformat 会忽略整秒部分为零的亚秒偏移，改变行情的可用时点。
    offset_fraction = re.search(r"[+-]\d{2}(?::?\d{2}){0,2}[.,](\d+)$", value)
    if offset_fraction and offset_fraction[1].strip("0"):
        raise ValueError("行情时刻的时区偏移须精确到整秒")
    return result.astimezone(UTC)


def text(record: dict, key: str) -> str:
    value = record[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 须为非空字符串")
    return value


def decimal_value(value: str, *, precision: int, positive: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise TypeError("数值须用十进制字符串表示")
    number = Decimal(value)
    if (
        not number.is_finite()
        or (positive and number <= 0)
        or (number != 0 and number.adjusted() >= precision - 8)
    ):
        raise ValueError(f"数值不符合 DECIMAL({precision},8) 的范围")
    normalized = Context(prec=precision).quantize(number, Decimal("0.00000001"))
    if number != normalized:
        raise ValueError(f"数值不符合 DECIMAL({precision},8) 的精度")
    return normalized


_TABLES = {
    "listings": ("market_listings", 6),
    "symbols": ("market_symbols", 5),
    "pool_changes": ("market_pool_changes", 6),
    "session_status": ("market_session_status", 5),
    "adjusted_prices": ("market_adjusted_prices", 6),
    "fundamentals": ("market_fundamentals", 8),
}


def parse_metadata(
    manifest: dict,
    bars: list[dict],
    closes: dict[date, datetime],
) -> dict[str, list[tuple]]:
    """用同一日历和证券有效期校验各类数据，计算缺行覆盖摘要。"""
    text(manifest, "provider")
    if manifest["price_basis"] != "raw":
        raise ValueError("历史市场批次须提供原始行情，调整价另存")
    source = manifest["source_ref"]
    zone = ZoneInfo(manifest["exchange_timezone"])
    records: dict[str, list[tuple]] = {name: [] for name in _TABLES}
    ids = {
        item["instrument_key"]: UUID(item["instrument_id"])
        for item in manifest["instruments"]
    }
    lifecycles = {}

    for index, item in enumerate(manifest["instruments"]):
        instrument = ids[item["instrument_key"]]
        listed = date.fromisoformat(item["listed_on"])
        listing_at = timestamp(item["listing_available_at"])
        delisted = item.get("delisted_on")
        delisting_at = item.get("delisting_available_at")
        if (delisted is None) != (delisting_at is None):
            raise ValueError("退市日与退市消息可用时间须成对提供")
        end = date.fromisoformat(delisted) if delisted is not None else None
        end_at = timestamp(delisting_at) if delisting_at is not None else None
        if end is not None and (end <= listed or end_at < listing_at):
            raise ValueError("上市和退市顺序无效")
        lifecycles[instrument] = (listed, end)
        records["listings"].append(
            (
                instrument,
                listed,
                listing_at,
                end,
                end_at,
                f"{source}#instruments/{index}",
            )
        )

    def require_active(instrument: UUID, day: date) -> None:
        start, end = lifecycles[instrument]
        if day < start or (end is not None and day >= end):
            raise ValueError(f"{instrument} 在 {day} 不处于上市区间")

    observed = {}
    for item in bars:
        instrument = ids[item["instrument_key"]]
        day = date.fromisoformat(item["session_date"])
        require_active(instrument, day)
        if item.get("adj_close") is not None:
            raise ValueError("调整价必须写入 adjusted_prices 并提供独立可用时间")
        observed[instrument, day] = item["volume"]

    for name in _TABLES.keys() - {"listings"}:
        items = manifest.pop(name, [])
        if not isinstance(items, list):
            raise TypeError(f"{name} 须为 JSON 数组")
        for index, item in enumerate(items):
            instrument = ids[item["instrument_key"]]
            available = timestamp(item["available_at"])
            ref = f"{source}#{name}/{index}"
            if name in ("symbols", "pool_changes"):
                day = date.fromisoformat(item["effective_on"])
            elif name != "fundamentals":
                day = date.fromisoformat(item["session_date"])
                if day not in closes:
                    raise ValueError(f"{day} 不在交易日历")
                require_active(instrument, day)

            if name == "symbols":
                require_active(instrument, day)
                row = (instrument, day, available, text(item, "symbol"), ref)
            elif name == "pool_changes":
                member = item["is_member"]
                if type(member) is not bool:
                    raise TypeError("is_member 须为布尔值")
                if member:
                    require_active(instrument, day)
                row = (text(item, "pool_name"), instrument, day, available, member, ref)
            elif name == "session_status":
                if item["status"] not in ("halted", "no_trade"):
                    raise ValueError("交易状态须为 halted 或 no_trade")
                if observed.get((instrument, day)):
                    raise ValueError("全日停牌或无交易状态与正成交量冲突")
                if available.astimezone(zone).date() < day:
                    raise ValueError("全日交易状态不得在该交易日前发布")
                row = (instrument, day, available, item["status"], ref)
            elif name == "adjusted_prices":
                if available < closes[day] or (instrument, day) not in observed:
                    raise ValueError("调整价格须关联已收盘的原始观察")
                row = (
                    instrument,
                    day,
                    available,
                    decimal_value(item["adjusted_close"], precision=20, positive=True),
                    text(item, "method"),
                    ref,
                )
            else:
                period_end = date.fromisoformat(item["period_end"])
                disclosed = timestamp(item["disclosed_at"])
                if (
                    disclosed.astimezone(zone).date() < period_end
                    or available < disclosed
                ):
                    raise ValueError("财报时序须满足报告期结束、披露、可用")
                row = (
                    instrument,
                    text(item, "metric"),
                    period_end,
                    disclosed,
                    available,
                    decimal_value(item["value"], precision=28),
                    text(item, "unit"),
                    ref,
                )
            records[name].append(row)

    _check_symbol_history(records["symbols"], lifecycles)
    _check_series_units(records)
    days = list(closes)
    expected = sum(
        (bisect_left(days, end) if end is not None else len(days))
        - bisect_left(days, start)
        for start, end in lifecycles.values()
    )
    explained = observed.keys() | {
        (row[0], row[1]) for row in records["session_status"]
    }
    manifest["integrity"] = {
        "expected_sessions": expected,
        "observed_bars": len(bars),
        "explained_sessions": len(explained),
        "unexplained_gaps": expected - len(explained),
        "explicit_statuses": len(records["session_status"]),
    }
    return records


def _check_symbol_history(symbols: list[tuple], lifecycles: dict) -> None:
    by_instrument = defaultdict(list)
    for instrument, day, _, symbol, _ in symbols:
        by_instrument[instrument].append((day, symbol))
    by_symbol = defaultdict(list)
    for instrument, (listed, delisted) in lifecycles.items():
        changes = sorted(by_instrument[instrument])
        if not changes or changes[0][0] != listed:
            raise ValueError(f"{instrument} 缺少上市时的证券代码")
        for (start, symbol), (end, _) in pairwise(
            [*changes, (delisted or date.max, "")]
        ):
            if start >= end:
                raise ValueError(f"{instrument} 的证券代码生效日重复或无效")
            by_symbol[symbol].append((start, end))
    for symbol, ranges in by_symbol.items():
        if any(
            end > next_start for (_, end), (next_start, _) in pairwise(sorted(ranges))
        ):
            raise ValueError(f"{symbol} 在同一日期分配给多个证券")


def _check_series_units(records: dict[str, list[tuple]]) -> None:
    methods = {row[4] for row in records["adjusted_prices"]}
    if len(methods) > 1:
        raise ValueError("同一批次只允许一种调整方法")
    units = {}
    for instrument, metric, _, _, _, _, unit, _ in records["fundamentals"]:
        if units.setdefault((instrument, metric), unit) != unit:
            raise ValueError(f"{instrument} 的 {metric} 单位不一致")


def write_metadata(
    connection: duckdb.DuckDBPyConnection, version: str, records: dict[str, list[tuple]]
) -> None:
    for name, (table, columns) in _TABLES.items():
        if records[name]:
            parameters = ", ".join(["?"] * (columns + 1))
            connection.executemany(
                f"INSERT INTO {table} VALUES ({parameters})",
                [(version, *row) for row in records[name]],
            )
