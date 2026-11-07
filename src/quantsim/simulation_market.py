"""从原始价格快照派生模拟市场，保留历史来源。"""

from dataclasses import dataclass, replace
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    InvalidOperation,
    Overflow,
)
from uuid import UUID

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.market_snapshot import RAW_PRICE_FIELDS, MarketSnapshot, MarketSnapshotRow


@dataclass(frozen=True)
class PriceShock:
    instrument_id: UUID
    multiplier: Decimal
    event_ref: str


def simulate_market(
    baseline: MarketSnapshot, shocks: tuple[PriceShock, ...] = ()
) -> MarketSnapshot:
    """按调用者给定的事件顺序调整价格，保留历史值及成交资格。"""
    if baseline.layer != "normalized":
        raise SimulatorError(
            ErrorCode.INVALID_LINEAGE, "模拟市场须从规范化原始行情快照派生"
        )
    rows = {row.instrument_id: row for row in baseline.rows}
    for shock in shocks:
        if shock.instrument_id not in rows:
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "冲击目标不在快照中")
        if (
            not isinstance(shock.multiplier, Decimal)
            or not shock.multiplier.is_finite()
            or shock.multiplier <= 0
            or not isinstance(shock.event_ref, str)
            or not shock.event_ref.strip()
        ):
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "冲击须有正乘数与事件来源")
        rows[shock.instrument_id] = _apply_shock(rows[shock.instrument_id], shock)
    return replace(baseline, layer="simulation", rows=tuple(rows.values()))


def _apply_shock(row: MarketSnapshotRow, shock: PriceShock) -> MarketSnapshotRow:
    context = Context(
        prec=40,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, Overflow],
    )
    quantum = Decimal("0.00000001")
    step = f"{shock.event_ref}: price × {context.to_sci_string(shock.multiplier)}"
    changed = []
    for field in row.fields:
        if field.name not in RAW_PRICE_FIELDS or field.value is None:
            changed.append(field)
            continue
        if field.lineage is None or not isinstance(field.value, Decimal):
            raise SimulatorError(ErrorCode.INVALID_LINEAGE, "模拟价格缺少原始价格来源")
        try:
            price = context.quantize(
                context.multiply(field.value, shock.multiplier), quantum
            )
            if not price.is_finite() or price <= 0 or price.adjusted() >= 12:
                raise ValueError("冲击后价格超出 DECIMAL(20,8) 范围")
        except (DecimalException, ValueError) as exc:
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "冲击后价格无效") from exc
        lineage = replace(
            field.lineage, transformations=(*field.lineage.transformations, step)
        )
        changed.append(replace(field, value=price, lineage=lineage))
    return replace(row, fields=tuple(changed))
