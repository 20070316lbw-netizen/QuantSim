"""验证冻结行情的导入和时点读取

_modified_fixture 生成修改后的临时数据包, 由导入错误测试调用
test_frozen_batch_reads_only_observations_published_by_cutoff 和 test_current_session_bars_do_not_use_late_or_prior_bars 检查可见性
test_duplicate_bars_roll_back_the_entire_import 检查失败回滚
test_batch_rejects_adjusted_prices_as_raw_bars、test_batch_rejects_non_array_input 和 test_batch_requires_current_historical_market_format 检查输入格式
test_fixture_price_precision_is_independent_of_caller_context 检查价格精度
"""

import json
from datetime import date, datetime
from decimal import Decimal, localcontext
from uuid import UUID

import duckdb
import pytest
from conftest import FIXTURE

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.simulation_clock import SimulationClock

A = UUID(int=1)
B = UUID(int=2)
JULY_2 = date(2024, 7, 2)
JULY_3 = date(2024, 7, 3)
JULY_5 = date(2024, 7, 5)


def test_frozen_batch_reads_only_observations_published_by_cutoff(market):
    version = market.import_batch(FIXTURE).data_version
    assert market.import_batch(FIXTURE).data_version == version

    sessions = market.sessions(version, JULY_2, JULY_5)
    assert tuple(session.session_date for session in sessions) == (
        JULY_2,
        JULY_3,
        JULY_5,
    )
    assert sessions[1].market_close_at == datetime.fromisoformat("2024-07-03T17:00:00Z")
    view = market.view(version, JULY_3)
    batch = view.history((A, B), lookback_sessions=2)
    assert batch.as_of == sessions[1].cutoff_at
    assert [(bar.session_date, bar.close) for bar in batch.bars] == [
        (JULY_2, Decimal(10)),
        (JULY_3, Decimal(11)),
    ]
    assert batch.bars[-1].volume == 0
    assert not batch.bars[-1].tradable

    later = market.view(version, JULY_5).history((B,), lookback_sessions=3)
    assert [bar.session_date for bar in later.bars] == [JULY_3]


def _modified_fixture(tmp_path, modify):
    source = json.loads(FIXTURE.read_text(encoding="utf-8"))
    modify(source)
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    return path


def test_duplicate_bars_roll_back_the_entire_import(market, tmp_path):
    bad = _modified_fixture(tmp_path, lambda data: data["bars"].append(data["bars"][0]))
    with pytest.raises(SimulatorError) as error:
        market.import_batch(bad)
    assert error.value.code is ErrorCode.INVALID_REQUEST
    assert isinstance(error.value.__cause__, duckdb.ConstraintException)
    assert market.versions() == ()

    version = market.import_batch(FIXTURE).data_version
    assert (
        len(market.view(version, JULY_5).history((A, B), lookback_sessions=3).bars) == 4
    )


def test_batch_rejects_adjusted_prices_as_raw_bars(market, tmp_path):
    source = _modified_fixture(
        tmp_path, lambda data: data.update(price_basis="adjusted")
    )
    with pytest.raises(SimulatorError, match="须提供原始行情") as error:
        market.import_batch(source)
    assert error.value.code is ErrorCode.INVALID_REQUEST
    assert market.versions() == ()


def test_current_session_bars_do_not_use_late_or_prior_bars(market):
    version = market.import_batch(FIXTURE).data_version
    clock = SimulationClock(market.sessions(version, JULY_2, JULY_5), committed_seq=1)
    tick = clock.peek()
    assert tick is not None
    batch = market.session_bars(version, (A, B), tick=tick)
    assert [(bar.instrument_id, bar.session_date) for bar in batch.bars] == [
        (A, JULY_3)
    ]


@pytest.mark.parametrize("field", ["bars", "sessions", "instruments"])
def test_batch_rejects_non_array_input(market, tmp_path, field):
    source = _modified_fixture(tmp_path, lambda data: data.update({field: {}}))

    with pytest.raises(SimulatorError) as error:
        market.import_batch(source)
    assert error.value.code is ErrorCode.INVALID_REQUEST


def test_fixture_price_precision_is_independent_of_caller_context(market):
    with localcontext(prec=5):
        version = market.import_batch(FIXTURE).data_version
        result = market.view(version, JULY_2).history((A,), lookback_sessions=1)
    assert result.bars[0].close == Decimal(10)


@pytest.mark.parametrize("format_version", [None, 1])
def test_batch_requires_current_historical_market_format(
    market, tmp_path, format_version
):
    def change_format(data):
        if format_version is None:
            del data["format_version"]
        else:
            data["format_version"] = format_version

    source = _modified_fixture(tmp_path, change_format)
    with pytest.raises(SimulatorError, match="format_version: 2") as error:
        market.import_batch(source)
    assert error.value.code is ErrorCode.INVALID_REQUEST
    assert market.versions() == ()
