"""验证日频时钟及恢复

test_clock_resumes_the_same_ticks_from_a_frozen_calendar 对照从头运行与恢复
test_clock_for_an_empty_calendar_is_finished 检查空日历
test_clock_rejects_invalid_advances_without_changing_its_position、test_clock_rejects_checkpoints_outside_the_calendar 和 test_clock_rejects_invalid_calendar_order_and_times 检查无效推进、检查点及日历
"""

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from conftest import FIXTURE

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.market_data import Session, Tick
from quantsim.simulation_clock import SimulationClock


def test_clock_resumes_the_same_ticks_from_a_frozen_calendar(market):
    version = market.import_batch(FIXTURE).data_version
    start, end = date(2024, 7, 2), date(2024, 7, 7)
    clock = SimulationClock(market.sessions(version, start, end))
    expected = [
        (1, date(2024, 7, 2), "2024-07-02T20:00:00Z"),
        (2, date(2024, 7, 3), "2024-07-03T17:00:00Z"),
        (3, date(2024, 7, 5), "2024-07-05T20:00:00Z"),
    ]
    for seq, day, close_at in expected:
        close = datetime.fromisoformat(close_at)
        assert clock.peek() == clock.peek() == Tick(day, close, close, seq)
        clock.advance(seq)

    resumed = SimulationClock(market.sessions(version, start, end), committed_seq=1)
    for seq, day, close_at in expected[1:]:
        close = datetime.fromisoformat(close_at)
        assert resumed.peek() == Tick(day, close, close, seq)
        resumed.advance(seq)

    assert clock.peek() is None
    assert resumed.peek() is None
    assert (
        SimulationClock(market.sessions(version, start, end), committed_seq=3).peek()
        is None
    )


def test_clock_for_an_empty_calendar_is_finished(market):
    version = market.import_batch(FIXTURE).data_version
    holiday = date(2024, 7, 4)
    clock = SimulationClock(market.sessions(version, holiday, holiday))
    assert clock.peek() is None
    with pytest.raises(SimulatorError) as error:
        clock.advance(1)
    assert error.value.code is ErrorCode.INCONSISTENT_STATE


def test_clock_rejects_invalid_advances_without_changing_its_position():
    close = datetime.fromisoformat("2024-07-03T17:00:00Z")
    cutoff = close + timedelta(minutes=5)
    session = Session(date(2024, 7, 3), close, cutoff)
    clock = SimulationClock((session,))
    for seq in (0, 2, True, 1.0):
        with pytest.raises(SimulatorError) as error:
            clock.advance(seq)
        assert error.value.code is ErrorCode.INCONSISTENT_STATE
        assert clock.peek() == Tick(session.session_date, close, cutoff, 1)

    clock.advance(1)
    for seq in (1, 2):
        with pytest.raises(SimulatorError) as error:
            clock.advance(seq)
        assert error.value.code is ErrorCode.INCONSISTENT_STATE
        assert clock.peek() is None


@pytest.mark.parametrize("seq", [-1, 1, True, 0.0])
def test_clock_rejects_checkpoints_outside_the_calendar(seq):
    with pytest.raises(SimulatorError) as error:
        SimulationClock((), committed_seq=seq)
    assert error.value.code is ErrorCode.INVALID_REQUEST


def test_clock_rejects_invalid_calendar_order_and_times():
    day = date(2024, 7, 3)
    close = datetime.fromisoformat("2024-07-03T17:00:00Z")
    session = Session(day, close, close + timedelta(minutes=10))
    following = Session(
        day + timedelta(days=2), close + timedelta(days=2), close + timedelta(days=2)
    )
    for sessions in (
        (session, replace(following, session_date=day)),
        (session, replace(following, market_close_at=close)),
        (
            session,
            replace(
                following,
                market_close_at=close + timedelta(minutes=1),
                cutoff_at=session.cutoff_at,
            ),
        ),
        (replace(session, cutoff_at=close - timedelta(minutes=1)),),
        (replace(session, market_close_at=close.replace(tzinfo=None)),),
    ):
        with pytest.raises(SimulatorError) as error:
            SimulationClock(sessions)
        assert error.value.code is ErrorCode.INVALID_REQUEST
