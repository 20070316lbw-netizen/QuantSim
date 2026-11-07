"""根据当前原始行情和账户约束计算订单终态

execute 校验订单、账户和配置, 通过 MarketView.snapshot 获取当日可见的原始行情
函数按提交顺序检查场景、成交资格、容量和购买力, 返回成交或结束原因及预计算后的现金和持仓
内部 money 按货币单位半偶舍入成交额和费用, 输入状态不被修改
结果尚未写入账本或数据库, 由后续回放协调器负责提交
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import (
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Literal
from uuid import UUID

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.market_data import MarketView, Tick
from quantsim.scenario import ScenarioState


@dataclass(frozen=True)
class Order:
    order_id: UUID
    instrument_id: UUID
    side: Literal["buy", "sell"]
    quantity: int
    submitted_tick: int
    target_tick: int
    intent_seq: int


@dataclass(frozen=True)
class ExecutionConfig:
    participation_rate: Decimal
    fee_rate: Decimal
    currency_unit: Decimal = Decimal("0.01")


@dataclass(frozen=True)
class PortfolioState:
    cash: Decimal
    positions: dict[UUID, int]


@dataclass(frozen=True)
class Fill:
    order_id: UUID
    instrument_id: UUID
    side: Literal["buy", "sell"]
    quantity: int
    price: Decimal
    notional: Decimal
    fee: Decimal
    bar_source_ref: str
    data_version: str
    session_date: date
    executed_at: datetime


@dataclass(frozen=True)
class OrderOutcome:
    order_id: UUID
    reason: str
    fill: Fill | None = None


@dataclass(frozen=True)
class ExecutionResult:
    outcomes: tuple[OrderOutcome, ...]
    cash: Decimal
    positions: dict[UUID, int]


def execute(
    tick: Tick,
    orders: tuple[Order, ...],
    market: MarketView,
    limits: ScenarioState,
    state: PortfolioState,
    config: ExecutionConfig,
) -> ExecutionResult:
    """按固定顺序全成或结束, 返回待提交结果, 不修改输入

    成交额舍入为零时以 zero_notional 结束, 不消耗现金、股数或容量
    """
    session = market.session
    if (session.session_date, session.market_close_at, session.cutoff_at) != (
        tick.session_date,
        tick.market_close_at,
        tick.cutoff_at,
    ):
        raise SimulatorError(
            ErrorCode.INCONSISTENT_STATE, "撮合行情视图须绑定当前 tick"
        )
    if (
        not isinstance(config.participation_rate, Decimal)
        or not config.participation_rate.is_finite()
        or not 0 <= config.participation_rate <= 1
        or not isinstance(config.fee_rate, Decimal)
        or not config.fee_rate.is_finite()
        or not 0 <= config.fee_rate <= 1
        or not isinstance(config.currency_unit, Decimal)
        or not config.currency_unit.is_finite()
        or config.currency_unit <= 0
    ):
        raise SimulatorError(ErrorCode.INVALID_REQUEST, "撮合费率、容量或货币单位无效")
    if (
        not isinstance(state.cash, Decimal)
        or not state.cash.is_finite()
        or state.cash < 0
        or any(
            not isinstance(k, UUID) or type(v) is not int or v < 0
            for k, v in state.positions.items()
        )
    ):
        raise SimulatorError(ErrorCode.INVALID_REQUEST, "账户现金或持仓无效")
    ids, keys = set(), set()
    for order in orders:
        key = (order.submitted_tick, order.intent_seq)
        if (
            not isinstance(order.order_id, UUID)
            or not isinstance(order.instrument_id, UUID)
            or order.order_id in ids
            or key in keys
            or order.side not in ("buy", "sell")
            or type(order.quantity) is not int
            or order.quantity <= 0
            or type(order.submitted_tick) is not int
            or order.submitted_tick < 1
            or type(order.intent_seq) is not int
            or order.intent_seq < 0
            or type(order.target_tick) is not int
            or order.target_tick != order.submitted_tick + 1
        ):
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST, "订单结构、顺序或目标 tick 无效"
            )
        ids.add(order.order_id)
        keys.add(key)
    due = sorted(
        (o for o in orders if o.target_tick == tick.seq),
        key=lambda o: (o.submitted_tick, o.intent_seq),
    )
    instruments = tuple(dict.fromkeys(o.instrument_id for o in due))
    snapshot = market.snapshot(pool=instruments, fields=("close", "volume", "tradable"))
    rows = {row.instrument_id: row for row in snapshot.rows}
    cash, positions, capacity, outcomes = state.cash, dict(state.positions), {}, []
    # 隔离调用方的精度、舍入、指数范围和异常设置
    context = Context(
        prec=80,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )
    with localcontext(context):

        def money(value: Decimal) -> Decimal:
            return (value / config.currency_unit).quantize(
                Decimal(1), rounding=ROUND_HALF_EVEN
            ) * config.currency_unit

        for order in due:
            row = rows[order.instrument_id]
            close, volume, tradable = (
                row.field(name) for name in ("close", "volume", "tradable")
            )
            reason = None
            if not limits.allows_trading(order.instrument_id) or row.status in (
                "halted",
                "no_trade",
                "not_listed",
                "delisted",
            ):
                reason = "not_tradable"
            elif close.value is None or volume.value is None:
                reason = "no_bar"
            elif volume.value == 0:
                reason = "zero_volume"
            elif tradable.value is False or row.status == "non_tradable":
                reason = "not_tradable"
            else:
                if order.instrument_id not in capacity:
                    capacity[order.instrument_id] = int(
                        (
                            Decimal(volume.value) * config.participation_rate
                        ).to_integral_value(rounding=ROUND_FLOOR)
                    )
                if order.quantity > capacity[order.instrument_id]:
                    reason = "insufficient_capacity"
                else:
                    raw = close.value * order.quantity
                    notional, fee = money(raw), money(raw * config.fee_rate)
                    if notional == 0:
                        reason = "zero_notional"
                    elif order.side == "buy" and cash < notional + fee:
                        reason = "insufficient_cash"
                    elif (
                        order.side == "sell"
                        and positions.get(order.instrument_id, 0) < order.quantity
                    ):
                        reason = "insufficient_position"
                    elif order.side == "sell" and cash + notional < fee:
                        reason = "insufficient_cash"
            if reason is not None:
                outcomes.append(OrderOutcome(order.order_id, reason))
                continue
            capacity[order.instrument_id] -= order.quantity
            if order.side == "buy":
                cash -= notional + fee
                positions[order.instrument_id] = (
                    positions.get(order.instrument_id, 0) + order.quantity
                )
            else:
                cash += notional - fee
                positions[order.instrument_id] -= order.quantity
            fill = Fill(
                order.order_id,
                order.instrument_id,
                order.side,
                order.quantity,
                close.value,
                notional,
                fee,
                close.lineage.source_ref,
                market.data_version,
                tick.session_date,
                tick.market_close_at,
            )
            outcomes.append(OrderOutcome(order.order_id, "filled", fill))
    return ExecutionResult(tuple(outcomes), cash, positions)
