"""按冻结的交易日历推进日频回放。"""

from datetime import timedelta
from itertools import pairwise

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.market_data import Session, Tick


class SimulationClock:
    """按回放区间内的交易日编号，从最后一个已提交 tick 恢复。"""

    def __init__(
        self, sessions: tuple[Session, ...], *, committed_seq: int = 0
    ) -> None:
        self._sessions = tuple(sessions)
        if type(committed_seq) is not int or not 0 <= committed_seq <= len(
            self._sessions
        ):
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "已提交序号超出回放范围")

        if any(
            session.market_close_at.utcoffset() != timedelta(0)
            or session.cutoff_at.utcoffset() != timedelta(0)
            or session.cutoff_at < session.market_close_at
            for session in self._sessions
        ):
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST,
                "收盘和 cutoff 须为 UTC，且 cutoff 不早于收盘",
            )
        if any(
            left.session_date >= right.session_date
            or left.market_close_at >= right.market_close_at
            or left.cutoff_at >= right.cutoff_at
            for left, right in pairwise(self._sessions)
        ):
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "交易日历须严格递增")
        self._committed_seq = committed_seq

    def peek(self) -> Tick | None:
        """读取下一未提交的 tick，读到区间末尾时返回 None。"""
        if self._committed_seq == len(self._sessions):
            return None
        session = self._sessions[self._committed_seq]
        return Tick(
            session.session_date,
            session.market_close_at,
            session.cutoff_at,
            seq=self._committed_seq + 1,
        )

    def advance(self, committed_seq: int) -> None:
        """在调用方确认当前 tick 已持久化后推进。"""
        expected_seq = self._committed_seq + 1
        if (
            type(committed_seq) is not int
            or committed_seq != expected_seq
            or expected_seq > len(self._sessions)
        ):
            raise SimulatorError(
                ErrorCode.INCONSISTENT_STATE,
                f"无法推进至 tick {committed_seq}，已提交至 {self._committed_seq}",
            )
        self._committed_seq = committed_seq
