"""为三日回放验收产生固定的买入和清仓意图。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.market_data import MarketView, Tick
from quantsim.scenario import ScenarioState


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
        """第一 tick 从空仓买入，第二 tick 根据实际持仓清仓。"""
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
