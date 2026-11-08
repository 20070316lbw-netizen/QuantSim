"""验收公开回放入口的事务回滚、精确账本、恢复和重复提交

测试使用冻结的三日行情, 故障注入只影响临时数据库
"""

import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import duckdb
import pytest
from conftest import FIXTURE

from quantsim import replay
from quantsim.database import initialize_database
from quantsim.errors import ErrorCode, SimulatorError
from quantsim.execution import ExecutionConfig
from quantsim.market_data import MarketDataRepository
from quantsim.replay import Replay, ReplaySpec
from quantsim.scenario import ScenarioEvent
from quantsim.strategy import FixedRoundTripStrategy, OrderIntent


@pytest.fixture
def replay_inputs(tmp_path):
    path = tmp_path / "replay.duckdb"
    initialize_database(path)
    source = Path(__file__).resolve().parents[1] / "examples" / "round_trip.json"
    version = MarketDataRepository(path).import_batch(source).data_version
    spec = ReplaySpec(
        simulation_id=uuid4(),
        data_version=version,
        start_session=date(2024, 7, 2),
        end_session=date(2024, 7, 5),
        initial_cash=Decimal(1000),
        execution=ExecutionConfig(Decimal(1), Decimal("0.001")),
        strategy=FixedRoundTripStrategy(UUID(int=1), 10),
        toolkit_revision="test-fixture",
        lock_hash="test-fixture",
    )
    return path, spec


def test_transaction_retry_and_resume(replay_inputs, monkeypatch):
    path, spec = replay_inputs
    run = Replay.create(path, spec)
    assert run.next_tick().seq == 1
    with pytest.raises(SimulatorError, match="不能跳过"):
        run.step(2)
    first = run.step(1)
    assert first.cash == first.equity == Decimal(1000)
    assert first.positions == ()

    original = replay._insert_event

    def fail_before_checkpoint(
        db, run, tick_seq, event_seq, event_type, *args, **kwargs
    ):
        if event_type == "tick_committed" and tick_seq == 2:
            assert db.execute("SELECT count(*) FROM fills").fetchone() == (1,)
            assert db.execute("SELECT count(*) FROM cash_ledger").fetchone() == (2,)
            assert db.execute("SELECT count(*) FROM positions_daily").fetchone() == (1,)
            assert db.execute("SELECT count(*) FROM orders").fetchone() == (2,)
            db.execute("INSERT INTO schema_info (version) VALUES (NULL)")
        original(db, run, tick_seq, event_seq, event_type, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(replay, "_insert_event", fail_before_checkpoint)
        with pytest.raises(SimulatorError) as error:
            run.step(2)
        assert error.value.code == ErrorCode.DATA_UNAVAILABLE

    with duckdb.connect(path, read_only=True) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM fills").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM positions_daily").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM cash_ledger").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM events WHERE tick_seq = 2"
        ).fetchone() == (0,)

    resumed = Replay(path, spec.simulation_id)
    assert resumed.next_tick().seq == 2
    assert resumed.step(1) == first
    second = resumed.step(2)
    assert second.cash == Decimal("889.89")
    assert second.equity == Decimal("999.89")
    (position,) = second.positions
    assert (position.quantity, position.market_value, position.bar_source_ref) == (
        10,
        Decimal(110),
        "fixture:round_trip#bars/1",
    )
    assert run.step(2) == second
    third = run.step(3)
    assert third.cash == third.equity == Decimal("1009.77")
    assert third.positions == ()
    assert resumed.next_tick() is None
    assert resumed.step(2) == second
    assert resumed.step(3) == third

    with duckdb.connect(path, read_only=True) as db:
        assert db.execute(
            """
            SELECT l.tick_seq, l.amount, f.fee, o.side, o.target_tick, b.source_ref
            FROM cash_ledger l LEFT JOIN fills f USING (fill_id)
            LEFT JOIN orders o ON o.order_id = f.order_id
            LEFT JOIN market_bars b ON (b.data_version, b.instrument_id, b.session_date)
                                     = (f.data_version, f.instrument_id, f.session_date)
            ORDER BY l.tick_seq, l.entry_seq
            """
        ).fetchall() == [
            (0, Decimal(1000), None, None, None, None),
            (
                2,
                Decimal("-110.11"),
                Decimal("0.11"),
                "buy",
                2,
                "fixture:round_trip#bars/1",
            ),
            (
                3,
                Decimal("119.88"),
                Decimal("0.12"),
                "sell",
                3,
                "fixture:round_trip#bars/2",
            ),
        ]
        assert db.execute(
            "SELECT tick_seq FROM events WHERE event_type = 'run_finished'"
        ).fetchall() == [(3,)]
        assert db.execute(
            "SELECT count(*) FROM events WHERE event_type = 'tick_committed'"
        ).fetchone() == (3,)


def test_late_bar_closes_order_without_later_fill(replay_inputs, tmp_path):
    path, spec = replay_inputs
    data = json.loads(FIXTURE.read_text())
    data["bars"][1].update(
        volume=100, tradable=True, available_at="2024-07-03T17:15:00Z"
    )
    source = tmp_path / "late.json"
    source.write_text(json.dumps(data))
    version = MarketDataRepository(path).import_batch(source).data_version
    run = Replay.create(path, replace(spec, data_version=version))
    assert [run.step(seq).equity for seq in (1, 2, 3)] == [Decimal(1000)] * 3
    with duckdb.connect(path, read_only=True) as db:
        assert db.execute("SELECT count(*) FROM fills").fetchone() == (0,)
        assert db.execute(
            "SELECT tick_seq, payload->>'reason' FROM events WHERE event_type = 'order_closed'"
        ).fetchall() == [(2, "no_bar")]


def test_scenario_is_restored_and_final_tick_skips_strategy(replay_inputs, monkeypatch):
    path, spec = replay_inputs
    cutoff = datetime.fromisoformat("2024-07-03T17:00:00Z")
    event = ScenarioEvent(1, cutoff, cutoff, "halt")
    run = Replay.create(path, replace(spec, scenario_events=(event,)))
    run.step(1)
    assert run.step(2).cash == Decimal(1000)

    def unexpected_decision(*args):
        pytest.fail("最后一日仍调用策略")

    monkeypatch.setattr(FixedRoundTripStrategy, "decide", unexpected_decision)
    assert Replay(path, spec.simulation_id).step(3).cash == Decimal(1000)
    with duckdb.connect(path, read_only=True) as db:
        assert db.execute(
            """
            SELECT tick_seq, event_type, payload->>'reason'
            FROM events WHERE event_type IN ('scenario_applied', 'order_closed')
            ORDER BY tick_seq, event_seq
            """
        ).fetchall() == [
            (2, "scenario_applied", None),
            (2, "order_closed", "not_tradable"),
        ]


def test_order_requires_membership_in_frozen_batch(replay_inputs, monkeypatch):
    path, spec = replay_inputs
    run = Replay.create(path, spec)
    monkeypatch.setattr(
        FixedRoundTripStrategy,
        "decide",
        lambda *args: (OrderIntent(UUID(int=999), "buy", 1),),
    )
    with pytest.raises(SimulatorError, match="不属于冻结行情") as error:
        run.step(1)
    assert error.value.code == ErrorCode.INVALID_REQUEST
    assert run.next_tick().seq == 1
    with duckdb.connect(path, read_only=True) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone() == (0,)


def test_single_session_never_creates_unreachable_order(replay_inputs):
    path, spec = replay_inputs
    run = Replay.create(path, replace(spec, end_session=spec.start_session))
    assert run.step(1).equity == Decimal(1000)
    assert run.next_tick() is None
    with duckdb.connect(path, read_only=True) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone() == (0,)
        assert db.execute(
            "SELECT tick_seq FROM events WHERE event_type = 'run_finished'"
        ).fetchall() == [(1,)]
