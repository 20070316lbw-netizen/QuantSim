"""验收公开回放入口的事务回滚、精确账本、恢复和重复提交

测试使用冻结的三日行情, 故障注入只影响临时数据库
"""

import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from shutil import copyfile
from statistics import mean, stdev
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


def test_performance_from_committed_equity_and_fills(replay_inputs):
    path, spec = replay_inputs
    run = Replay.create(path, spec)
    empty = run.performance()
    assert (empty.complete, empty.days, empty.total_fees) == (False, (), Decimal(0))
    assert empty.portfolio.total_return is None
    with pytest.raises(SimulatorError) as error:
        run.report()
    assert error.value.code == ErrorCode.DATA_UNAVAILABLE

    run.step(1)
    partial = run.performance()
    assert not partial.complete
    assert partial.portfolio.total_return == 0
    assert partial.portfolio.sharpe is None
    assert partial.benchmark.total_return is None
    assert partial.days[0].benchmark_return is None
    assert run.next_tick().seq == 2
    run.step(2)
    run.step(3)
    result = Replay(path, spec.simulation_id).performance()
    assert result.complete
    assert (result.initial_equity, result.currency, result.total_fees) == (
        Decimal(1000),
        "USD",
        Decimal("0.23"),
    )
    assert [day.equity for day in result.days] == [
        Decimal(1000),
        Decimal("999.89"),
        Decimal("1009.77"),
    ]
    assert [day.fees for day in result.days] == [
        Decimal(0),
        Decimal("0.11"),
        Decimal("0.12"),
    ]
    daily = [0, -0.11 / 1000, 9.88 / 999.89]
    assert [day.portfolio_return for day in result.days] == pytest.approx(daily)
    assert result.portfolio.total_return == pytest.approx(0.00977)
    assert result.portfolio.max_drawdown == pytest.approx(daily[1])
    assert result.portfolio.sharpe == pytest.approx(mean(daily) / stdev(daily))
    assert result.portfolio.win_rate == pytest.approx(1 / 3)
    assert result.benchmark.total_return == pytest.approx(0.2)
    assert result.benchmark_missing_sessions == ()
    assert run.performance() == result


def test_report_renders_equity_and_benchmark(replay_inputs, tmp_path, monkeypatch):
    monkeypatch.setenv("MPLBACKEND", "Agg")
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path))
    from matplotlib import pyplot as plt
    from matplotlib import rc_context

    path, spec = replay_inputs
    run = Replay.create(path, spec)
    for seq in (1, 2, 3):
        run.step(seq)
    output = tmp_path / "report.png"
    with rc_context({"savefig.format": "png"}):
        figure = run.report(output=output.with_suffix(""))
    try:
        nav, drawdown = figure.axes
        assert nav.lines[0].get_ydata() == pytest.approx([1, 0.99989, 1.00977])
        assert nav.lines[-1].get_ydata() == pytest.approx([1, 1.1, 1.2])
        assert drawdown.lines[0].get_ydata() == pytest.approx([0, -0.00011, 0])
        assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        plt.close(figure)
    with pytest.raises(SimulatorError) as error:
        run.report(output=tmp_path / "missing" / "report.png")
    assert error.value.code == ErrorCode.DATA_UNAVAILABLE
    assert run.next_tick() is None


@pytest.mark.parametrize("alias", ["hardlink", "default_extension"])
def test_report_rejects_database_alias(replay_inputs, tmp_path, monkeypatch, alias):
    monkeypatch.setenv("MPLBACKEND", "Agg")
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path))
    from matplotlib import rc_context

    path, spec = replay_inputs
    if alias == "hardlink":
        output = tmp_path / "report.png"
    else:
        output = tmp_path / "report"
        path = path.rename(output.with_suffix(".png"))
    run = Replay.create(path, spec)
    run.step(1)
    if alias == "hardlink":
        output.hardlink_to(path)
    before = path.read_bytes()
    with rc_context({"savefig.format": "png"}), pytest.raises(SimulatorError) as error:
        run.report(output=output)
    assert error.value.code is ErrorCode.INVALID_REQUEST
    assert path.read_bytes() == before
    assert Replay(path, spec.simulation_id).next_tick().seq == 2


def test_record_rejects_generated_report_path_matching_database(
    replay_inputs, tmp_path, monkeypatch
):
    monkeypatch.setenv("MPLBACKEND", "Agg")
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path))
    path, spec = replay_inputs
    run = Replay.create(path, spec)
    run.step(1)
    record = run.record(report_directory=tmp_path)

    directory = tmp_path / "collision"
    directory.mkdir()
    collision = directory / record.report_path.name
    copyfile(path, collision)
    before = collision.read_bytes()
    reopened = Replay(collision, spec.simulation_id)
    with pytest.raises(SimulatorError) as error:
        reopened.record(report_directory=directory)
    assert error.value.code is ErrorCode.INVALID_REQUEST
    assert collision.read_bytes() == before
    assert reopened.records() == (record,)
    assert tuple(directory.iterdir()) == (collision,)


def test_record_persists_prefix_and_rebuilds_report(
    replay_inputs, tmp_path, monkeypatch
):
    monkeypatch.setenv("MPLBACKEND", "Agg")
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path))
    path, spec = replay_inputs
    run = Replay.create(path, spec)
    with pytest.raises(SimulatorError) as error:
        run.record()
    assert error.value.code is ErrorCode.DATA_UNAVAILABLE
    assert run.records() == ()

    run.step(1)
    prefix = run.record()
    assert (prefix.tick_seq, prefix.end_session, prefix.complete) == (
        1,
        spec.start_session,
        False,
    )
    assert prefix.report_path is None
    assert run.next_tick().seq == 2
    run.step(2)
    run.step(3)
    with pytest.raises(SimulatorError) as error:
        run.record(report_directory=tmp_path / "missing")
    assert error.value.code is ErrorCode.DATA_UNAVAILABLE
    assert run.records() == (prefix,)

    record = run.record(report_directory=tmp_path)
    assert record.complete
    assert record.total_fees == Decimal("0.23")
    assert record.portfolio.total_return == pytest.approx(0.00977)
    assert record.benchmark.total_return == pytest.approx(0.2)
    assert record.report_path.suffix == ".png"
    assert sha256(record.report_path.read_bytes()).hexdigest() == record.report_sha256
    reopened = Replay(path, spec.simulation_id)
    assert reopened.records() == (prefix, record)
    assert reopened.record() == record
    assert reopened.record(report_directory=tmp_path) == record
    with pytest.raises(SimulatorError):
        reopened.record(report_directory=tmp_path / "missing")
    assert reopened.records() == (prefix, record)
    record.report_path.unlink()
    assert reopened.records() == (prefix, record)
    assert reopened.record(report_directory=tmp_path) == record
    assert record.report_path.is_file()
    assert reopened.next_tick() is None


def test_saved_configuration_reruns_after_version_two_upgrade(replay_inputs):
    path, spec = replay_inputs
    # 使用版本 2 的表集和迁移历史保存前两日, 升级后继续
    with duckdb.connect(path) as db:
        db.execute("DROP TABLE replay_results")
        db.execute("DELETE FROM schema_migrations WHERE version = 3")
        db.execute("UPDATE schema_info SET version = 2")
    cutoff = datetime.fromisoformat("2024-07-05T20:00:00Z")
    event = ScenarioEvent(1, cutoff, cutoff, "resume")
    spec = replace(spec, seed=42, scenario_events=(event,))
    run = Replay.create(path, spec)
    run.step(1)
    run.step(2)
    before = run.performance()

    initialize_database(path)
    initialize_database(path)
    reopened = Replay(path, spec.simulation_id)
    assert reopened.spec == spec
    assert reopened.performance() == before
    assert reopened.records() == ()
    end = reopened.step(3)
    record = reopened.record()

    second = Replay.create(path, replace(reopened.spec, simulation_id=uuid4()))
    for seq in (1, 2, 3):
        last = second.step(seq)
    assert last == end
    assert replace(second.record(), simulation_id=spec.simulation_id) == record

    queries = (
        """
        SELECT * EXCLUDE (simulation_id, order_id) FROM orders
        WHERE simulation_id = ? ORDER BY submitted_tick, intent_seq
        """,
        """
        SELECT f.* EXCLUDE (simulation_id, fill_id, order_id), o.submitted_tick, o.intent_seq
        FROM fills f JOIN orders o USING (order_id)
        WHERE f.simulation_id = ? ORDER BY f.tick_seq, o.submitted_tick, o.intent_seq
        """,
        """
        SELECT l.* EXCLUDE (simulation_id, fill_id), o.submitted_tick, o.intent_seq
        FROM cash_ledger l LEFT JOIN fills f USING (fill_id) LEFT JOIN orders o USING (order_id)
        WHERE l.simulation_id = ? ORDER BY l.tick_seq, l.entry_seq
        """,
        """
        SELECT tick_seq, payload FROM events WHERE simulation_id = ?
        AND event_type = 'tick_committed' ORDER BY tick_seq
        """,
    )
    with duckdb.connect(path, read_only=True) as db:
        for query in queries:
            assert (
                db.execute(query, [spec.simulation_id]).fetchall()
                == db.execute(query, [second.spec.simulation_id]).fetchall()
            )


def test_performance_rejects_a_gap_in_checkpoints(replay_inputs):
    path, spec = replay_inputs
    run = Replay.create(path, spec)
    for seq in (1, 2, 3):
        run.step(seq)
    with duckdb.connect(path) as db:
        db.execute("DELETE FROM events WHERE tick_seq = 2 AND event_seq = 0")
    with pytest.raises(SimulatorError, match="检查点不连续") as error:
        run.performance()
    assert error.value.code == ErrorCode.INCONSISTENT_STATE


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
