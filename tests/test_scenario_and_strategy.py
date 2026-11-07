"""验证场景事件顺序与固定策略

test_scenario_applies_in_order_and_waits_for_effective_and_available_times 检查事件生效、发布和排序
test_scenario_rejects_a_late_old_event_that_would_reverse_a_newer_state 检查迟到事件顺序
test_fixed_strategy_uses_the_current_view_and_actual_positions 检查当前行情、持仓及未来数据隔离
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from conftest import FIXTURE

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.market_data import Tick
from quantsim.scenario import ScenarioEvent, ScenarioEventEngine, ScenarioState
from quantsim.simulation_clock import SimulationClock
from quantsim.strategy import FixedRoundTripStrategy, OrderIntent, StrategyContext

A = UUID(int=1)
B = UUID(int=2)
START = date(2024, 7, 2)
END = date(2024, 7, 5)


def test_scenario_applies_in_order_and_waits_for_effective_and_available_times(market):
    version = market.import_batch(FIXTURE).data_version
    clock = SimulationClock(market.sessions(version, START, END))
    first = clock.peek()
    at = first.market_close_at
    halt = ScenarioEvent(1, at, at, "halt", A)
    resume = ScenarioEvent(2, at, at, "resume", A)
    later = at + timedelta(minutes=1)
    late_notice = ScenarioEvent(3, at, later, "halt", B)
    early_notice = ScenarioEvent(4, later, at, "halt")
    pending = (resume, early_notice, late_notice, halt)

    initial = ScenarioState()
    first_update = ScenarioEventEngine.apply(first, initial, pending)
    assert first_update.applied == (halt, resume)
    assert first_update.pending == (late_notice, early_notice)
    assert first_update.state == initial
    assert ScenarioEventEngine.apply(first, initial, pending[::-1]) == first_update

    clock.advance(first.seq)
    second = clock.peek()
    end = ScenarioEventEngine.apply(second, first_update.state, first_update.pending)
    assert end.applied == (late_notice, early_notice)
    assert not end.pending
    assert not end.state.allows_trading(A)
    assert not end.state.allows_trading(B)

    market_resume = ScenarioEvent(5, second.market_close_at, second.cutoff_at, "resume")
    resumed = ScenarioEventEngine.apply(second, end.state, (market_resume,))
    assert resumed.state.allows_trading(A)
    assert not resumed.state.allows_trading(B)


def test_scenario_rejects_a_late_old_event_that_would_reverse_a_newer_state(market):
    version = market.import_batch(FIXTURE).data_version
    tick = SimulationClock(market.sessions(version, START, END)).peek()
    now = tick.market_close_at
    late_halt = ScenarioEvent(
        1, now - timedelta(minutes=1), now + timedelta(days=1), "halt", A
    )
    resume = ScenarioEvent(2, now, now, "resume", A)
    with pytest.raises(SimulatorError) as error:
        ScenarioEventEngine.apply(tick, ScenarioState(), (resume, late_halt))
    assert error.value.code is ErrorCode.INVALID_REQUEST


def test_scenario_rejects_invalid_event_types():
    at = datetime(2024, 7, 2, 20, tzinfo=UTC)
    tick = Tick(at.date(), at, at, 1)
    halt = ScenarioEvent(1, at, at, "halt", A)
    for event in (
        replace(halt, instrument_id=str(A)),
        replace(halt, effective_at=at.isoformat()),
        replace(halt, available_at=None),
        None,
    ):
        with pytest.raises(SimulatorError) as error:
            ScenarioEventEngine.apply(tick, ScenarioState(), (event,))
        assert error.value.code is ErrorCode.INVALID_REQUEST


def test_fixed_strategy_uses_the_current_view_and_actual_positions(market, tmp_path):
    original = market.import_batch(FIXTURE).data_version
    bundle = json.loads(FIXTURE.read_bytes())
    bundle["bars"][-1].update(open="35", high="35", low="35", close="35")
    changed = tmp_path / "future_changed.json"
    changed.write_text(json.dumps(bundle), encoding="utf-8")
    revised = market.import_batch(changed).data_version

    strategy = FixedRoundTripStrategy(A, 10)
    for version in (original, revised):
        clock = SimulationClock(market.sessions(version, START, END))
        first = clock.peek()
        view = market.view(version, first.session_date)
        state = ScenarioState()
        assert strategy.decide(StrategyContext(first, view, {}, state)) == (
            OrderIntent(A, "buy", 10),
        )
        halted = ScenarioState(frozenset({A}))
        assert strategy.decide(StrategyContext(first, view, {}, halted)) == ()

        clock.advance(first.seq)
        second = clock.peek()
        second_view = market.view(version, second.session_date)
        assert strategy.decide(StrategyContext(second, second_view, {}, state)) == ()
        # 当日 bar 虽不可成交, 仍可作为下一 tick 清仓决策的观察
        assert strategy.decide(
            StrategyContext(second, second_view, {A: 10}, state)
        ) == (OrderIntent(A, "sell", 10),)
        assert (
            FixedRoundTripStrategy(B, 10).decide(
                StrategyContext(second, second_view, {B: 10}, state)
            )
            == ()
        )
        with pytest.raises(SimulatorError, match="当前 tick"):
            strategy.decide(StrategyContext(first, second_view, {}, state))

        clock.advance(second.seq)
        third = clock.peek()
        third_view = market.view(version, third.session_date)
        assert strategy.decide(StrategyContext(third, third_view, {A: 10}, state)) == ()
