"""为日频回放生成固定买卖或动量调仓意图

FixedRoundTripStrategy.decide 在第一 tick 买入, 第二 tick 按实际持仓清仓
MomentumRotationStrategy.decide 通过 Adapter 对可见的历史收益或动量与低波动合成分数排名
OrderIntent 是策略输出, 撮合由独立模块决定
"""

from collections.abc import Mapping
from dataclasses import dataclass
from math import isclose, isfinite
from typing import Literal
from uuid import UUID

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.market_data import MarketView, Tick
from quantsim.scenario import ScenarioState
from quantsim.toolkit_adapter import QuantToolkitAdapter


@dataclass(frozen=True)
class OrderIntent:
    instrument_id: UUID
    side: Literal["buy", "sell"]
    quantity: int


@dataclass(frozen=True)
class StrategyContext:
    tick: Tick
    market: MarketView
    positions: Mapping[UUID, int]
    limits: ScenarioState


@dataclass(frozen=True)
class FixedRoundTripStrategy:
    instrument_id: UUID
    quantity: int

    def __post_init__(self) -> None:
        if type(self.quantity) is not int or self.quantity <= 0:
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "策略股数须为正整数")

    def decide(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        """第一 tick 从空仓买入, 第二 tick 根据实际持仓清仓"""
        _check_market_tick(context)
        tick = context.tick

        instrument = self.instrument_id
        if tick.seq not in (1, 2) or not context.limits.allows_trading(instrument):
            return ()
        if not context.market.history((instrument,), lookback_sessions=1).bars:
            return ()

        held = context.positions.get(instrument, 0)
        if tick.seq == 1 and held == 0:
            return (OrderIntent(instrument, "buy", self.quantity),)
        if tick.seq == 2 and held > 0:
            return (OrderIntent(instrument, "sell", held),)
        return ()


@dataclass(frozen=True)
class MomentumRotationStrategy:
    """按固定交易日间隔, 持有策略特征排名第一的证券及固定目标股数

    输入为冻结批次内的证券 UUID、回看期数、调仓间隔和目标股数
    设置 volatility_window 后, 同时使用动量和低波动合成分数
    无有效分数或现有持仓当日不可交易时跳过, 成交仍由下一 tick 的撮合决定
    """

    instruments: tuple[UUID, ...]
    n_periods: int
    rebalance_every: int
    quantity: int
    volatility_window: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.instruments, tuple)
            or not self.instruments
            or any(not isinstance(instrument, UUID) for instrument in self.instruments)
            or len(set(self.instruments)) != len(self.instruments)
            or type(self.n_periods) is not int
            or not 1 <= self.n_periods < 2**31
            or type(self.rebalance_every) is not int
            or self.rebalance_every < 1
            or type(self.quantity) is not int
            or not 0 < self.quantity < 2**63
            or (
                self.volatility_window is not None
                and (
                    type(self.volatility_window) is not int
                    or not 2 <= self.volatility_window < 2**31
                )
            )
        ):
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "动量调仓配置无效")
        object.__setattr__(self, "instruments", tuple(sorted(self.instruments)))

    def decide(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        """以 tick 1 为调仓起点, 按分数降序和 UUID 升序选股, 先卖后买"""
        _check_market_tick(context)
        tick = context.tick
        lookback = max(self.n_periods, self.volatility_window or 0) + 1
        if (tick.seq - 1) % self.rebalance_every or len(
            context.market.sessions(lookback_sessions=lookback)
        ) < lookback:
            return ()

        snapshot = context.market.snapshot(
            pool=self.instruments, fields=("volume", "tradable")
        )
        eligible = tuple(
            row.instrument_id
            for row in snapshot.rows
            if row.status == "trading"
            and row.field("tradable").value is True
            and (row.field("volume").value or 0) > 0
            and context.limits.allows_trading(row.instrument_id)
        )
        if not eligible or context.positions.keys() - set(eligible):
            return ()

        feature = "panel.returns.historical_return"
        params = {"n_periods": self.n_periods}
        if self.volatility_window is not None:
            feature = "factors.combine.combine_scores"
            params["window"] = self.volatility_window
        result = (
            QuantToolkitAdapter()
            .for_strategy(market=context.market)
            .feature(
                feature,
                eligible,
                lookback_sessions=lookback,
                params=params,
            )
        )
        scores = [
            (-point.value, point.instrument_id)
            for point in result.points
            if point.session_date == tick.session_date
            and point.value is not None
            and isfinite(point.value)
        ]
        if not scores:
            return ()

        best_score, target = min(scores)
        if self.volatility_window is not None:
            # 消除标准化和合成运算产生的舍入误差
            target = min(
                instrument
                for score, instrument in scores
                if isclose(score, best_score, rel_tol=0.0, abs_tol=1e-12)
            )
        intents = [
            OrderIntent(instrument, "sell", held)
            for instrument, held in sorted(context.positions.items())
            if instrument != target
        ]
        difference = self.quantity - context.positions.get(target, 0)
        if difference > 0:
            intents.append(OrderIntent(target, "buy", difference))
        elif difference < 0:
            intents.append(OrderIntent(target, "sell", -difference))
        return tuple(intents)


def _check_market_tick(context: StrategyContext) -> None:
    session = context.market.session
    tick = context.tick
    if (session.session_date, session.market_close_at, session.cutoff_at) != (
        tick.session_date,
        tick.market_close_at,
        tick.cutoff_at,
    ):
        raise SimulatorError(
            ErrorCode.INCONSISTENT_STATE, "策略行情视图须绑定当前 tick"
        )
