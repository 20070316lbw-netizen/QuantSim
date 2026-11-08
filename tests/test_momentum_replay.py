"""用两只证券验收动量调仓、时点限制与运行恢复"""

import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import duckdb
import pytest
from conftest import FIXTURE

from quantsim.database import initialize_database
from quantsim.errors import ErrorCode, SimulatorError
from quantsim.execution import ExecutionConfig
from quantsim.market_data import MarketDataRepository
from quantsim.replay import Replay, ReplaySpec
from quantsim.scenario import ScenarioEvent
from quantsim.strategy import MomentumRotationStrategy

A = UUID(int=1)
B = UUID(int=2)


@pytest.fixture
def momentum_inputs(tmp_path):
    data = json.loads(FIXTURE.read_text())
    data["sessions"].extend(
        {
            "session_date": f"2024-07-{day:02}",
            "market_close_at": f"2024-07-{day:02}T20:00:00Z",
            "cutoff_at": f"2024-07-{day:02}T20:00:00Z",
        }
        for day in (8, 9, 10)
    )
    data["bars"] = [
        {
            "instrument_key": symbol,
            "session_date": session["session_date"],
            "available_at": session["cutoff_at"],
            **dict.fromkeys(("open", "high", "low", "close"), str(price)),
            "volume": 100,
            "tradable": True,
        }
        for symbol, prices in (
            ("A", (10, 10, 12, 12, 11, 11)),
            ("B", (10, 10, 10, 10, 16, 16)),
        )
        for session, price in zip(data["sessions"], prices, strict=True)
    ]
    path = tmp_path / "momentum.duckdb"
    initialize_database(path)
    spec = ReplaySpec(
        simulation_id=uuid4(),
        data_version="",
        start_session=date(2024, 7, 2),
        end_session=date(2024, 7, 10),
        initial_cash=Decimal(200),
        execution=ExecutionConfig(Decimal(1), Decimal("0.001")),
        strategy=MomentumRotationStrategy((B, A), 1, 2, 10),
        toolkit_revision="test-fixture",
        lock_hash="test-fixture",
    )
    return path, tmp_path / "momentum.json", data, spec


def _create(inputs):
    path, source, data, spec = inputs
    source.write_text(json.dumps(data))
    version = MarketDataRepository(path).import_batch(source).data_version
    return Replay.create(path, replace(spec, data_version=version))


def _orders(path, simulation_id):
    with duckdb.connect(path, read_only=True) as db:
        return db.execute(
            """
            SELECT submitted_tick, target_tick, instrument_id, side, quantity
            FROM orders WHERE simulation_id = ? ORDER BY submitted_tick, intent_seq
            """,
            [simulation_id],
        ).fetchall()


def test_momentum_rotation_resumes_and_sells_before_buying(momentum_inputs):
    path, _, _, spec = momentum_inputs
    run = _create(momentum_inputs)
    for seq in (1, 2, 3):
        assert run.step(seq).positions == ()
    assert _orders(path, spec.simulation_id) == [(3, 4, A, "buy", 10)]

    resumed = Replay(path, spec.simulation_id)
    assert resumed.next_tick().seq == 4
    assert resumed.step(4).cash == Decimal("79.88")
    fifth = resumed.step(5)
    assert [p.instrument_id for p in fifth.positions] == [A]
    assert resumed.step(5) == fifth
    assert _orders(path, spec.simulation_id) == [
        (3, 4, A, "buy", 10),
        (5, 6, A, "sell", 10),
        (5, 6, B, "buy", 10),
    ]
    final = Replay(path, spec.simulation_id).step(6)
    assert [(p.instrument_id, p.quantity) for p in final.positions] == [(B, 10)]
    assert (final.cash, final.equity) == (Decimal("29.61"), Decimal("189.61"))
    assert resumed.next_tick() is None

    with duckdb.connect(path, read_only=True) as db:
        name, version, config = db.execute(
            "SELECT name, code_version, config FROM strategies"
        ).fetchone()
    assert (name, version) == ("momentum_rotation", "1")
    assert json.loads(config) == {
        "instruments": [str(A), str(B)],
        "n_periods": 1,
        "rebalance_every": 2,
        "quantity": 10,
    }


def test_composite_rotation_prefers_lower_volatility_and_resumes(momentum_inputs):
    path, source, data, spec = momentum_inputs
    # 两个调仓日的单期收益相同, A 的前一日收益变动更大
    prices = {
        ("A", "2024-07-02"): "5",
        ("A", "2024-07-05"): "11",
        ("B", "2024-07-05"): "11",
        ("A", "2024-07-08"): "5",
        ("A", "2024-07-09"): "5.5",
        ("B", "2024-07-09"): "11",
    }
    for bar in data["bars"]:
        key = (bar["instrument_key"], bar["session_date"])
        if key in prices:
            bar.update(dict.fromkeys(("open", "high", "low", "close"), prices[key]))
    momentum = _create(momentum_inputs)
    composite_spec = replace(
        spec,
        simulation_id=uuid4(),
        strategy=replace(spec.strategy, volatility_window=2),
    )
    composite = _create((path, source, data, composite_spec))

    for seq in (1, 2, 3):
        momentum.step(seq)
        composite.step(seq)
    assert _orders(path, spec.simulation_id) == [(3, 4, A, "buy", 10)]
    assert _orders(path, composite_spec.simulation_id) == [(3, 4, B, "buy", 10)]

    resumed = Replay(path, composite_spec.simulation_id)
    for seq in (4, 5, 6):
        resumed.step(seq)
    assert _orders(path, composite_spec.simulation_id) == [(3, 4, B, "buy", 10)]
    with duckdb.connect(path, read_only=True) as db:
        name, version, config = db.execute(
            """
            SELECT name, code_version, config
            FROM strategies JOIN simulations USING (strategy_id) WHERE simulation_id = ?
            """,
            [composite_spec.simulation_id],
        ).fetchone()
    assert (name, version) == ("momentum_rotation", "2")
    assert json.loads(config)["volatility_window"] == 2


def test_composite_rotation_breaks_mathematical_ties_by_uuid(momentum_inputs):
    path, source, data, spec = momentum_inputs
    spec = replace(spec, strategy=replace(spec.strategy, volatility_window=2))
    replay = _create((path, source, data, spec))
    for seq in (1, 2, 3, 4):
        replay.step(seq)
    assert _orders(path, spec.simulation_id) == [(3, 4, A, "buy", 10)]

    reopened = Replay(path, spec.simulation_id)
    for seq in (5, 6):
        snapshot = reopened.step(seq)
    assert [
        (position.instrument_id, position.quantity) for position in snapshot.positions
    ] == [(A, 10)]
    assert _orders(path, spec.simulation_id) == [(3, 4, A, "buy", 10)]


@pytest.mark.parametrize("condition", ["tie", "future", "late", "halt", "missing"])
def test_momentum_ranks_only_eligible_observations(momentum_inputs, condition):
    path, source, data, spec = momentum_inputs
    b_bars = {
        bar["session_date"]: bar for bar in data["bars"] if bar["instrument_key"] == "B"
    }
    b_today = b_bars["2024-07-05"]
    b_today.update(dict.fromkeys(("open", "high", "low", "close"), "15"))
    if condition == "tie":
        b_today.update(dict.fromkeys(("open", "high", "low", "close"), "12"))
    elif condition == "future":
        b_today.update(dict.fromkeys(("open", "high", "low", "close"), "10"))
        b_bars["2024-07-08"].update(
            dict.fromkeys(("open", "high", "low", "close"), "100")
        )
    elif condition == "late":
        b_today["available_at"] = "2024-07-05T20:01:00Z"
    elif condition == "halt":
        cutoff = datetime.fromisoformat("2024-07-05T20:00:00Z")
        spec = replace(
            spec, scenario_events=(ScenarioEvent(1, cutoff, cutoff, "halt", B),)
        )
    elif condition == "missing":
        data["bars"].remove(b_bars["2024-07-03"])

    run = _create((path, source, data, spec))
    for seq in (1, 2, 3):
        run.step(seq)
    assert _orders(path, spec.simulation_id) == [(3, 4, A, "buy", 10)]


def test_momentum_holds_when_existing_position_is_halted(momentum_inputs):
    path, source, data, spec = momentum_inputs
    cutoff = datetime.fromisoformat("2024-07-09T20:00:00Z")
    spec = replace(spec, scenario_events=(ScenarioEvent(1, cutoff, cutoff, "halt", A),))
    run = _create((path, source, data, spec))
    for seq in range(1, 6):
        run.step(seq)
    assert _orders(path, spec.simulation_id) == [(3, 4, A, "buy", 10)]
    assert [p.instrument_id for p in run.step(6).positions] == [A]


def test_momentum_rejects_instrument_outside_frozen_batch(momentum_inputs):
    path, source, data, spec = momentum_inputs
    spec = replace(spec, strategy=MomentumRotationStrategy((A, UUID(int=99)), 1, 2, 10))
    with pytest.raises(SimulatorError, match="不属于冻结行情") as error:
        _create((path, source, data, spec))
    assert error.value.code == ErrorCode.INVALID_REQUEST
