"""统一撮合和账本使用的账户状态与金额规则

PortfolioState 保存现金和证券股数, validate_state 检查只做多账户的输入
account_context 隔离调用方的 Decimal 设置, round_money 按账户货币单位半偶舍入
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from uuid import UUID

from quantsim.errors import ErrorCode, SimulatorError


@dataclass(frozen=True)
class PortfolioState:
    cash: Decimal
    positions: dict[UUID, int]


def validate_state(state: PortfolioState) -> None:
    if (
        not isinstance(state.cash, Decimal)
        or not state.cash.is_finite()
        or state.cash < 0
        or any(
            not isinstance(instrument, UUID)
            or type(quantity) is not int
            or quantity < 0
            for instrument, quantity in state.positions.items()
        )
    ):
        raise SimulatorError(ErrorCode.INVALID_REQUEST, "账户现金或持仓无效")


def valid_currency_unit(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def account_context() -> AbstractContextManager[Context]:
    return localcontext(
        Context(
            prec=80,
            rounding=ROUND_HALF_EVEN,
            Emin=-999999,
            Emax=999999,
            capitals=1,
            clamp=0,
            flags=[],
            traps=[InvalidOperation, DivisionByZero, Overflow],
        )
    )


def round_money(value: Decimal, currency_unit: Decimal) -> Decimal:
    """在账户 Decimal 上下文内按最小货币单位舍入"""
    return (value / currency_unit).quantize(Decimal(1)) * currency_unit
