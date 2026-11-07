"""验证独立撮合的订单终态和资源扣减

order 创建测试订单, setup_market 创建当日可成交的临时行情, 供撮合测试调用
test_fill_order_and_shared_cash 检查排序、费用和共享现金
test_capacity_positions_and_scenario 检查容量、可卖量、停牌及重复订单
test_missing_late_and_non_tradable_bar 检查零量和迟到行情
test_shared_capacity_and_sale_proceeds 检查共享容量和卖出款可用性
另覆盖零成交额和调用方 Decimal 上下文
"""

import json
from datetime import date
from decimal import ROUND_FLOOR, Decimal, Inexact, localcontext
from uuid import UUID

import pytest
from conftest import FIXTURE

from quantsim.errors import SimulatorError
from quantsim.execution import ExecutionConfig, Order, PortfolioState, execute
from quantsim.scenario import ScenarioState
from quantsim.simulation_clock import SimulationClock

A = UUID(int=1)
B = UUID(int=2)


def order(n, side="buy", quantity=10, instrument=A):
    return Order(UUID(int=n), instrument, side, quantity, 1, 2, n)


def setup_market(market, tmp_path, *, price="11", volume=100):
    data = json.loads(FIXTURE.read_text())
    data["bars"][1].update(
        open=price, high=price, low=price, close=price, volume=volume, tradable=True
    )
    path = tmp_path / "market.json"
    path.write_text(json.dumps(data))
    version = market.import_batch(path).data_version
    clock = SimulationClock(
        market.sessions(version, date(2024, 7, 2), date(2024, 7, 5))
    )
    clock.advance(1)
    tick = clock.peek()
    return tick, market.view(version, tick.session_date)


def test_fill_order_and_shared_cash(market, tmp_path):
    tick, view = setup_market(market, tmp_path)
    state = PortfolioState(Decimal(200), {})
    result = execute(
        tick,
        (order(2), order(1)),
        view,
        ScenarioState(),
        state,
        ExecutionConfig(Decimal(1), Decimal("0.001")),
    )
    assert [o.reason for o in result.outcomes] == ["filled", "insufficient_cash"]
    fill = result.outcomes[0].fill
    assert (fill.price, fill.notional, fill.fee) == (
        Decimal(11),
        Decimal(110),
        Decimal("0.11"),
    )
    assert fill.bar_source_ref and fill.data_version == view.data_version
    assert result.cash == Decimal("89.89") and result.positions == {A: 10}
    assert state.cash == Decimal(200) and state.positions == {}


def test_capacity_positions_and_scenario(market, tmp_path):
    tick, view = setup_market(market, tmp_path)
    state = PortfolioState(Decimal(1000), {A: 5})
    config = ExecutionConfig(Decimal(0), Decimal(0))
    assert (
        execute(tick, (order(1),), view, ScenarioState(), state, config)
        .outcomes[0]
        .reason
        == "insufficient_capacity"
    )
    config = ExecutionConfig(Decimal(1), Decimal(0))
    assert (
        execute(tick, (order(1, "sell"),), view, ScenarioState(), state, config)
        .outcomes[0]
        .reason
        == "insufficient_position"
    )
    assert (
        execute(tick, (order(1),), view, ScenarioState(frozenset({A})), state, config)
        .outcomes[0]
        .reason
        == "not_tradable"
    )
    with pytest.raises(SimulatorError):
        execute(tick, (order(1), order(1)), view, ScenarioState(), state, config)


def test_missing_late_and_non_tradable_bar(market):
    version = market.import_batch(FIXTURE).data_version
    clock = SimulationClock(
        market.sessions(version, date(2024, 7, 2), date(2024, 7, 5))
    )
    clock.advance(1)
    tick = clock.peek()
    view = market.view(version, tick.session_date)
    state = PortfolioState(Decimal(1000), {})
    config = ExecutionConfig(Decimal(1), Decimal(0))
    result = execute(
        tick, (order(1), order(2, instrument=B)), view, ScenarioState(), state, config
    )
    assert [outcome.reason for outcome in result.outcomes] == ["zero_volume", "no_bar"]


def test_shared_capacity_and_sale_proceeds(market, tmp_path):
    tick, view = setup_market(market, tmp_path)
    config = ExecutionConfig(Decimal("0.1"), Decimal(0))
    state = PortfolioState(Decimal(0), {A: 10})
    result = execute(
        tick, (order(2), order(1, "sell")), view, ScenarioState(), state, config
    )
    assert [outcome.reason for outcome in result.outcomes] == [
        "filled",
        "insufficient_capacity",
    ]
    assert result.cash == Decimal(110) and result.positions[A] == 0
    config = ExecutionConfig(Decimal(1), Decimal(0))
    result = execute(
        tick, (order(2), order(1, "sell")), view, ScenarioState(), state, config
    )
    assert [outcome.reason for outcome in result.outcomes] == ["filled", "filled"]
    assert result.cash == 0 and result.positions[A] == 10


@pytest.mark.parametrize(
    ("side", "cash", "positions", "expected_cash", "expected_positions"),
    [
        ("buy", "0.01", {}, "0", {A: 2}),
        ("sell", "0", {A: 2}, "0.01", {A: 0}),
    ],
)
def test_zero_notional_leaves_resources_available(
    market, tmp_path, side, cash, positions, expected_cash, expected_positions
):
    tick, view = setup_market(market, tmp_path, price="0.004", volume=2)
    result = execute(
        tick,
        (order(1, side, quantity=1), order(2, side, quantity=2)),
        view,
        ScenarioState(),
        PortfolioState(Decimal(cash), positions),
        ExecutionConfig(Decimal(1), Decimal(0)),
    )
    rejected, filled = result.outcomes
    assert (rejected.reason, rejected.fill) == ("zero_notional", None)
    assert filled.reason == "filled" and filled.fill.notional == Decimal("0.01")
    assert result.cash == Decimal(expected_cash)
    assert result.positions == expected_positions


def test_execution_is_independent_of_caller_decimal_context(market, tmp_path):
    tick, view = setup_market(market, tmp_path)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_FLOOR
        context.Emax = 1
        context.traps[Inexact] = True
        result = execute(
            tick,
            (order(1, quantity=1),),
            view,
            ScenarioState(),
            PortfolioState(Decimal(100), {}),
            ExecutionConfig(Decimal(1), Decimal("0.0005")),
        )
    assert result.outcomes[0].fill.fee == Decimal("0.01")
    assert result.cash == Decimal("88.99")
