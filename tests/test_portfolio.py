"""验证成交过账和日终估值的账户守恒

test_round_trip 复算三日买卖和费用, 并核对撮合预计算状态
test_mark_uses_latest_visible_raw_price 检查零量原始价、迟到行情与逐证券舍入
其余用例覆盖缺价、重复成交、余额不足和期初货币精度
"""

import json
from dataclasses import replace
from datetime import date
from decimal import ROUND_FLOOR, Decimal, Inexact, localcontext
from uuid import UUID

import pytest
from conftest import FIXTURE

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.execution import ExecutionConfig, Fill, Order, execute
from quantsim.portfolio import CashEntry, PortfolioState, apply_fills, initialize, mark
from quantsim.scenario import ScenarioState
from quantsim.simulation_clock import SimulationClock
from quantsim.strategy import FixedRoundTripStrategy, StrategyContext

A = UUID(int=1)
B = UUID(int=2)
DAYS = (date(2024, 7, 2), date(2024, 7, 3), date(2024, 7, 5))
CENT = Decimal("0.01")


def test_round_trip(market, tmp_path):
    data = json.loads(FIXTURE.read_text())
    data["bars"][1].update(volume=100, tradable=True)
    data["bars"][3].update(dict.fromkeys(("open", "high", "low", "close"), "12"))
    source = tmp_path / "round_trip.json"
    source.write_text(json.dumps(data))
    version = market.import_batch(source).data_version
    clock = SimulationClock(market.sessions(version, DAYS[0], DAYS[-1]))
    config = ExecutionConfig(Decimal(1), Decimal("0.001"))
    limits = ScenarioState()
    strategy = FixedRoundTripStrategy(A, 10)

    account = initialize(Decimal(1000), currency_unit=config.currency_unit)
    state = account.state
    assert account.entries == (CashEntry(Decimal(1000), None),)
    orders = ()
    snapshots, trade_entries = [], []
    while (tick := clock.peek()) is not None:
        view = market.view(version, tick.session_date)
        result = execute(tick, orders, view, limits, state, config)
        fills = tuple(outcome.fill for outcome in result.outcomes if outcome.fill)
        booked = apply_fills(state, fills)
        assert booked == apply_fills(state, fills)
        state = booked.state
        assert state.cash == result.cash
        assert state.positions == {
            key: qty for key, qty in result.positions.items() if qty
        }
        trade_entries.extend(booked.entries)
        snapshots.append(mark(tick, state, view, currency_unit=config.currency_unit))
        intents = strategy.decide(StrategyContext(tick, view, state.positions, limits))
        orders = tuple(
            Order(
                UUID(int=tick.seq),
                intent.instrument_id,
                intent.side,
                intent.quantity,
                tick.seq,
                tick.seq + 1,
                index,
            )
            for index, intent in enumerate(intents)
        )
        clock.advance(tick.seq)

    first, second, third = snapshots
    assert first.positions == third.positions == ()
    assert [snapshot.equity for snapshot in snapshots] == [
        Decimal("1000.00"),
        Decimal("999.89"),
        Decimal("1009.77"),
    ]
    (position,) = second.positions
    assert (
        position.quantity,
        position.mark_price,
        position.market_value,
        position.stale,
    ) == (10, Decimal(11), Decimal(110), False)
    assert second.cash == Decimal("889.89")
    assert third.cash == Decimal("1009.77") and state.positions == {}
    assert trade_entries == [
        CashEntry(Decimal("-110.11"), UUID(int=1)),
        CashEntry(Decimal("119.88"), UUID(int=2)),
    ]


def test_mark_uses_latest_visible_raw_price(market, tmp_path):
    data = json.loads(FIXTURE.read_text())
    prices = ("5.005", "11.005")
    for bar, price in zip(data["bars"], prices):
        bar.update(dict.fromkeys(("open", "high", "low", "close"), price))
    data["bars"].append({**data["bars"][0], "instrument_key": "B"})
    source = tmp_path / "late_bar.json"
    source.write_text(json.dumps(data))
    version = market.import_batch(source).data_version
    state = PortfolioState(Decimal(100), {B: 1, A: 1})
    clock = SimulationClock(
        market.sessions(version, DAYS[0], DAYS[-1]), committed_seq=1
    )
    view = market.view(version, DAYS[1])
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_FLOOR
        context.traps[Inexact] = True
        snapshot = mark(clock.peek(), state, view, currency_unit=CENT)

    current, stale = snapshot.positions
    assert snapshot.data_version == version
    assert (current.instrument_id, current.mark_session, current.stale) == (
        A,
        DAYS[1],
        False,
    )
    assert current.mark_price == Decimal("11.005")
    assert current.market_value == Decimal("11.00")
    assert (stale.instrument_id, stale.mark_session, stale.stale) == (B, DAYS[0], True)
    assert stale.mark_price == Decimal("5.005")
    assert stale.market_value == Decimal("5.00")
    assert stale.bar_source_ref == "fixture:market#bars/4"
    assert snapshot.equity == Decimal("116.00")

    clock.advance(2)
    view = market.view(version, DAYS[2])
    snapshot = mark(clock.peek(), state, view, currency_unit=CENT)
    _, late = snapshot.positions
    assert (late.mark_session, late.mark_price, late.stale) == (
        DAYS[1],
        Decimal(20),
        True,
    )


def test_mark_requires_visible_price(market):
    version = market.import_batch(FIXTURE).data_version
    view = market.view(version, DAYS[1])
    clock = SimulationClock(
        market.sessions(version, DAYS[0], DAYS[-1]), committed_seq=1
    )
    state = PortfolioState(Decimal(100), {B: 1})
    with pytest.raises(SimulatorError) as exc:
        mark(clock.peek(), state, view, currency_unit=CENT)
    assert exc.value.code == ErrorCode.DATA_UNAVAILABLE
    with pytest.raises(SimulatorError) as exc:
        mark(clock.peek(), state, market.view(version, DAYS[2]), currency_unit=CENT)
    assert exc.value.code == ErrorCode.INCONSISTENT_STATE


def test_booking_checks_each_fill_and_leaves_input_unchanged(market):
    version = market.import_batch(FIXTURE).data_version
    view = market.view(version, DAYS[2])
    fill = Fill(
        UUID(int=1),
        A,
        "buy",
        1,
        Decimal(22),
        Decimal(22),
        Decimal("0.10"),
        "fixture:market#bars/3",
        version,
        DAYS[2],
        view.session.market_close_at,
    )
    state = PortfolioState(Decimal(30), {})
    for fills, message in (
        ((fill, fill), "重复"),
        ((fill, replace(fill, order_id=UUID(int=2))), "不足"),
        ((replace(fill, side="sell"), fill), "不足"),
    ):
        with pytest.raises(SimulatorError, match=message) as exc:
            apply_fills(state, fills)
        assert exc.value.code == ErrorCode.INCONSISTENT_STATE
        assert state == PortfolioState(Decimal(30), {})


def test_initial_entry_uses_currency_unit():
    account = initialize(Decimal("10.125"), currency_unit=Decimal("0.05"))
    assert account.state.cash == Decimal("10.10")
    assert account.entries == (CashEntry(Decimal("10.10"), None),)
    with pytest.raises(SimulatorError, match="舍入后须大于零"):
        initialize(Decimal("0.005"), currency_unit=CENT)
