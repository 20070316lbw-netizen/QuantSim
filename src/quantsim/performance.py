"""定义日频回放绩效结果, 并从已提交权益与可见收盘价构造收益

Replay 提供连续的检查点及冻结日历, 本模块保留缺期并通过 Adapter 调用工具箱
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import file_digest, sha256
from itertools import pairwise
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING
from uuid import UUID

import pandas as pd

from quantsim._account import account_context
from quantsim.errors import ErrorCode, SimulatorError
from quantsim.market_data import MarketDataRepository, Session
from quantsim.toolkit_adapter import (
    PerformanceMetrics,
    replay_benchmark,
    replay_metrics,
    replay_report,
)

if TYPE_CHECKING:
    from matplotlib.figure import Figure


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


@dataclass(frozen=True)
class ReplayRecord:
    """与一个已提交区间和基准证券池对应的持久化绩效摘要"""

    simulation_id: UUID
    tick_seq: int
    end_session: date
    complete: bool
    benchmark_instruments: tuple[UUID, ...]
    benchmark_missing_sessions: tuple[date, ...]
    currency: str
    total_fees: Decimal
    portfolio: PerformanceMetrics
    benchmark: PerformanceMetrics
    report_path: Path | None
    report_sha256: str | None


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


def render_report(
    result: PerformanceResult,
    *,
    output: Path | None,
    database_path: Path | None = None,
) -> "Figure":
    """渲染指定已提交区间的收益, 基准从第二日开始"""
    portfolio, benchmark = report_series(result)
    if portfolio.empty or portfolio.isna().any():
        raise SimulatorError(ErrorCode.DATA_UNAVAILABLE, "报告需要连续的策略收益")
    return replay_report(
        portfolio, benchmark, output=output, database_path=database_path
    )


def publish_report(result: PerformanceResult, directory: Path) -> tuple[Path, str]:
    """在同一目录渲染临时 PNG, 按内容摘要发布并保留已有文件"""
    try:
        with NamedTemporaryFile(dir=directory, suffix=".png", delete=False) as file:
            temporary = Path(file.name)
        try:
            figure = render_report(result, output=temporary)
            from matplotlib import pyplot as plt

            plt.close(figure)
            digest = sha256(temporary.read_bytes()).hexdigest()
            output = directory / f"{result.simulation_id}-{digest}.png"
            try:
                output.hardlink_to(temporary)
            except FileExistsError:
                same_size = (
                    not output.is_symlink()
                    and output.is_file()
                    and output.stat().st_size == temporary.stat().st_size
                )
                if same_size:
                    with output.open("rb") as existing:
                        if file_digest(existing, "sha256").hexdigest() == digest:
                            return output, digest
                raise SimulatorError(
                    ErrorCode.INVALID_REQUEST, "报告目标已存在且内容不同"
                ) from None
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise SimulatorError(ErrorCode.DATA_UNAVAILABLE, "无法发布回放报告") from exc
    return output, digest
