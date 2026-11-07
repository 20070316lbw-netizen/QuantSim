"""从已确认的成交计算现金流水、持仓和日终权益

initialize 生成初始账户及入账流水, apply_fills 按成交顺序过账并返回新状态
mark 使用当前 MarketView 的最近可见原始行情估值, 保留标记价及其来源
回放协调器负责持久化流水和快照
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, DecimalException
from uuid import UUID

from quantsim._account import (
    PortfolioState,
    account_context,
    round_money,
    valid_currency_unit,
    validate_state,
)
from quantsim.errors import ErrorCode, SimulatorError
from quantsim.execution import Fill
from quantsim.market_data import MarketView, Tick


@dataclass(frozen=True)
class CashEntry:
    amount: Decimal
    order_id: UUID | None


@dataclass(frozen=True)
class BookingResult:
    state: PortfolioState
    entries: tuple[CashEntry, ...]


@dataclass(frozen=True)
class PositionMark:
    instrument_id: UUID
    quantity: int
    mark_session: date
    mark_price: Decimal
    market_value: Decimal
    bar_source_ref: str
    stale: bool


@dataclass(frozen=True)
class PortfolioSnapshot:
    tick: Tick
    data_version: str
    cash: Decimal
    positions: tuple[PositionMark, ...]
    equity: Decimal


def initialize(initial_cash: Decimal, *, currency_unit: Decimal) -> BookingResult:
    """按账户货币单位舍入期初现金, 生成一笔无订单引用的初始流水

    金额和货币单位须为有限 Decimal, 舍入后现金须大于零
    """
    if (
        not isinstance(initial_cash, Decimal)
        or not initial_cash.is_finite()
        or initial_cash <= 0
        or not valid_currency_unit(currency_unit)
    ):
        raise SimulatorError(ErrorCode.INVALID_REQUEST, "期初现金或货币单位无效")
    try:
        with account_context():
            cash = round_money(initial_cash, currency_unit)
    except DecimalException as exc:
        raise SimulatorError(ErrorCode.INVALID_REQUEST, "期初现金无法舍入") from exc
    if cash <= 0:
        raise SimulatorError(ErrorCode.INVALID_REQUEST, "期初现金舍入后须大于零")
    return BookingResult(PortfolioState(cash, {}), (CashEntry(cash, None),))


def apply_fills(state: PortfolioState, fills: tuple[Fill, ...]) -> BookingResult:
    """按顺序过账当期撮合的成交, 每笔成交生成一笔净现金流水

    同一批内订单 ID 须唯一, 余额及股数在每笔成交后均须非负
    状态不一致时整批失败, 调用方以原状态重算, 跨 tick 去重由协调器负责
    """
    validate_state(state)
    cash = state.cash
    positions = {
        instrument: shares for instrument, shares in state.positions.items() if shares
    }
    entries: list[CashEntry] = []
    booked_orders: set[UUID] = set()
    with account_context():
        for fill in fills:
            if (
                not isinstance(fill.order_id, UUID)
                or fill.order_id in booked_orders
                or not isinstance(fill.instrument_id, UUID)
                or fill.side not in ("buy", "sell")
                or type(fill.quantity) is not int
                or fill.quantity <= 0
                or not isinstance(fill.notional, Decimal)
                or not fill.notional.is_finite()
                or fill.notional <= 0
                or not isinstance(fill.fee, Decimal)
                or not fill.fee.is_finite()
                or fill.fee < 0
            ):
                raise SimulatorError(
                    ErrorCode.INCONSISTENT_STATE, "成交重复或过账字段无效"
                )

            instrument = fill.instrument_id
            shares = positions.get(instrument, 0)
            if fill.side == "buy":
                amount = -(fill.notional + fill.fee)
                shares += fill.quantity
            else:
                amount = fill.notional - fill.fee
                shares -= fill.quantity
            cash += amount
            if cash < 0 or shares < 0:
                raise SimulatorError(
                    ErrorCode.INCONSISTENT_STATE, "过账后现金或持仓不足"
                )
            if shares:
                positions[instrument] = shares
            else:
                del positions[instrument]
            booked_orders.add(fill.order_id)
            entries.append(CashEntry(amount, fill.order_id))

    return BookingResult(PortfolioState(cash, positions), tuple(entries))


def mark(
    tick: Tick, state: PortfolioState, market: MarketView, *, currency_unit: Decimal
) -> PortfolioSnapshot:
    """用当期或最近可见的原始收盘价估值, 过去的价格标记为 stale

    逐证券市值舍入后与现金相加, 任一非零持仓缺少可追溯价格时估值失败
    """
    validate_state(state)
    if not valid_currency_unit(currency_unit):
        raise SimulatorError(ErrorCode.INVALID_REQUEST, "估值货币单位无效")
    session = market.session
    if (session.session_date, session.market_close_at, session.cutoff_at) != (
        tick.session_date,
        tick.market_close_at,
        tick.cutoff_at,
    ):
        raise SimulatorError(
            ErrorCode.INCONSISTENT_STATE, "估值行情视图须绑定当前 tick"
        )

    instruments = tuple(
        sorted(key for key, shares in state.positions.items() if shares)
    )
    bars = (
        {bar.instrument_id: bar for bar in market.latest_bars(instruments).bars}
        if instruments
        else {}
    )
    positions: list[PositionMark] = []
    equity = state.cash
    try:
        with account_context():
            for instrument in instruments:
                bar = bars.get(instrument)
                if bar is None:
                    raise SimulatorError(
                        ErrorCode.DATA_UNAVAILABLE,
                        f"持仓 {instrument} 缺少可见的原始收盘价",
                    )
                quantity = state.positions[instrument]
                value = round_money(bar.close * quantity, currency_unit)
                positions.append(
                    PositionMark(
                        instrument,
                        quantity,
                        bar.session_date,
                        bar.close,
                        value,
                        bar.source_ref,
                        bar.session_date < tick.session_date,
                    )
                )
                equity += value
    except DecimalException as exc:
        raise SimulatorError(ErrorCode.INCONSISTENT_STATE, "持仓市值无法舍入") from exc
    return PortfolioSnapshot(
        tick, market.data_version, state.cash, tuple(positions), equity
    )
