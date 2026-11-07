"""按事件生效时间和可用时间维护交易限制

ScenarioEventEngine.apply 排序并应用当前 tick 可见的 halt / resume 事件, 返回新状态及待处理事件
ScenarioState.allows_trading 同时检查全市场和单证券限制, 供策略及撮合调用
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.market_data import Tick


@dataclass(frozen=True)
class ScenarioEvent:
    scenario_sequence: int
    effective_at: datetime
    available_at: datetime
    action: Literal["halt", "resume"]
    instrument_id: UUID | None = None


@dataclass(frozen=True)
class ScenarioState:
    """保存独立的停牌对象。None 表示全市场停牌。"""

    halted_targets: frozenset[UUID | None] = frozenset()

    def allows_trading(self, instrument_id: UUID) -> bool:
        return (
            None not in self.halted_targets and instrument_id not in self.halted_targets
        )


@dataclass(frozen=True)
class ScenarioUpdate:
    state: ScenarioState
    applied: tuple[ScenarioEvent, ...]
    pending: tuple[ScenarioEvent, ...]


class ScenarioEventEngine:
    @staticmethod
    def apply(
        tick: Tick, state: ScenarioState, pending: tuple[ScenarioEvent, ...]
    ) -> ScenarioUpdate:
        """在当前 tick 应用已生效且已发布的事件，保留其余事件。"""
        sequences = set()
        for event in pending:
            if (
                type(event.scenario_sequence) is not int
                or event.scenario_sequence < 0
                or event.scenario_sequence in sequences
                or event.action not in ("halt", "resume")
                or event.effective_at.utcoffset() != timedelta(0)
                or event.available_at.utcoffset() != timedelta(0)
            ):
                raise SimulatorError(
                    ErrorCode.INVALID_REQUEST, "场景事件的序号、动作或时刻无效"
                )
            sequences.add(event.scenario_sequence)

        ordered = sorted(
            pending, key=lambda event: (event.effective_at, event.scenario_sequence)
        )
        last_available: dict[UUID | None, datetime] = {}
        halted = set(state.halted_targets)
        applied = []
        waiting = []
        for event in ordered:
            target = event.instrument_id
            if target in last_available and event.available_at < last_available[target]:
                raise SimulatorError(
                    ErrorCode.INVALID_REQUEST, "同一对象的事件可用时间须随执行顺序递增"
                )
            last_available[target] = event.available_at
            if (
                event.effective_at > tick.market_close_at
                or event.available_at > tick.cutoff_at
            ):
                waiting.append(event)
                continue
            if event.action == "halt":
                halted.add(target)
            else:
                halted.discard(target)
            applied.append(event)
        return ScenarioUpdate(
            ScenarioState(frozenset(halted)), tuple(applied), tuple(waiting)
        )
