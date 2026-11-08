"""定义日频回放绩效结果, 并从已提交权益与可见收盘价构造收益

Replay 提供连续的检查点及冻结日历, 本模块保留缺期并通过 Adapter 调用工具箱
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from uuid import UUID

import pandas as pd

from quantsim._account import account_context
from quantsim.market_data import MarketDataRepository, Session
from quantsim.toolkit_adapter import (
    PerformanceMetrics,
    replay_benchmark,
    replay_metrics,
)


@dataclass(frozen=True)
class DailyPerformance:
    session_date: date
    equity: Decimal
    fees: Decimal
    portfolio_return: float | None
    benchmark_return: float | None


@dataclass(frozen=True)
class PerformanceResult:
    simulation_id: UUID
    data_version: str
    currency: str
    initial_equity: Decimal
    total_fees: Decimal
    complete: bool
    benchmark_instruments: tuple[UUID, ...]
    benchmark_missing_sessions: tuple[date, ...]
    days: tuple[DailyPerformance, ...]
    portfolio: PerformanceMetrics
    benchmark: PerformanceMetrics


def _portfolio_returns(
    initial_equity: Decimal, equities: tuple[Decimal, ...]
) -> tuple[float | None, ...]:
    with account_context():
        return tuple(
            float(current / previous - 1) if previous > 0 else None
            for previous, current in pairwise((initial_equity, *equities))
        )


def _benchmark_returns(
    market: MarketDataRepository,
    version: str,
    sessions: tuple[Session, ...],
    instruments: tuple[UUID, ...],
) -> tuple[tuple[float | None, ...], tuple[date, ...]]:
    prices = []
    missing = []
    for session in sessions:
        day = session.session_date
        batch = market.history(
            version, instruments, start=day, end=day, as_of=session.cutoff_at
        )
        closes = {bar.instrument_id: float(bar.close) for bar in batch.bars}
        if len(closes) < len(instruments):
            missing.append(day)
        prices.extend((day, str(key), closes.get(key)) for key in instruments)

    if not sessions or (missing and missing[0] == sessions[0].session_date):
        return (None,) * len(sessions), tuple(missing)
    panel = pd.DataFrame(prices, columns=["date", "ticker", "close"])
    returns = replay_benchmark(panel)
    return tuple(None if pd.isna(value) else float(value) for value in returns), tuple(
        missing
    )


def _series(sessions: tuple[date, ...], values: tuple[float | None, ...]) -> pd.Series:
    return pd.Series(values, index=pd.DatetimeIndex(sessions), dtype=float)


def build_performance(
    *,
    simulation_id: UUID,
    version: str,
    currency: str,
    initial_equity: Decimal,
    market: MarketDataRepository,
    sessions: tuple[Session, ...],
    equities: tuple[Decimal, ...],
    fees: tuple[Decimal, ...],
    benchmark_instruments: tuple[UUID, ...],
    complete: bool,
) -> PerformanceResult:
    portfolio = _portfolio_returns(initial_equity, equities)
    benchmark, missing = _benchmark_returns(
        market, version, sessions, benchmark_instruments
    )
    dates = tuple(session.session_date for session in sessions)
    with account_context():
        total_fees = sum(fees, Decimal(0))
    return PerformanceResult(
        simulation_id=simulation_id,
        data_version=version,
        currency=currency,
        initial_equity=initial_equity,
        total_fees=total_fees,
        complete=complete,
        benchmark_instruments=benchmark_instruments,
        benchmark_missing_sessions=missing,
        days=tuple(
            DailyPerformance(*row)
            for row in zip(dates, equities, fees, portfolio, benchmark, strict=True)
        ),
        portfolio=replay_metrics(_series(dates, portfolio)),
        benchmark=replay_metrics(_series(dates[1:], benchmark[1:])),
    )


def report_series(result: PerformanceResult) -> tuple[pd.Series, pd.Series]:
    """构造报告使用的策略与基准收益, 基准从第二日开始"""
    dates = tuple(day.session_date for day in result.days)
    portfolio = _series(dates, tuple(day.portfolio_return for day in result.days))
    benchmark = _series(
        dates[1:], tuple(day.benchmark_return for day in result.days[1:])
    )
    return portfolio, benchmark
