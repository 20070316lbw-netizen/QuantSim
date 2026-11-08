"""逐日编排场景、撮合、账本与策略, 并保存可恢复的 DuckDB 检查点

Replay.create 冻结运行配置并原子写入期初流水
Replay.next_tick 查询下一待处理步骤, Replay.step 按显式序号提交或读回既有快照
Replay.performance 和 Replay.report 只读取已提交区间, 不推进运行
Replay.record 和 Replay.records 保存和读取已提交区间的绩效及报告引用
恢复只依赖数据库中的运行配置、检查点及已提交业务记录
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5

import duckdb

from quantsim import portfolio
from quantsim._account import PortfolioState, account_context
from quantsim.errors import ErrorCode, SimulatorError
from quantsim.execution import (
    ExecutionConfig,
    Order,
    OrderOutcome,
    execute,
    validate_execution_config,
)
from quantsim.market_data import MarketDataRepository, Tick
from quantsim.performance import (
    PerformanceResult,
    ReplayRecord,
    build_performance,
    publish_report,
    render_report,
)
from quantsim.portfolio import BookingResult, PortfolioSnapshot, PositionMark
from quantsim.scenario import ScenarioEvent, ScenarioEventEngine, ScenarioState
from quantsim.simulation_clock import SimulationClock
from quantsim.strategy import (
    FixedRoundTripStrategy,
    MomentumRotationStrategy,
    OrderIntent,
    StrategyContext,
)
from quantsim.toolkit_adapter import PerformanceMetrics

if TYPE_CHECKING:
    from matplotlib.figure import Figure

_FIXED_ROUND_TRIP = "fixed_round_trip"
_MOMENTUM_ROTATION = "momentum_rotation"
_STRATEGY_VERSION = "1"
_COMPOSITE_STRATEGY_VERSION = "2"
_SCENARIO_VERSION = "halt_resume:1"


@dataclass(frozen=True)
class ReplaySpec:
    simulation_id: UUID
    data_version: str
    start_session: date
    end_session: date
    initial_cash: Decimal
    execution: ExecutionConfig
    strategy: FixedRoundTripStrategy | MomentumRotationStrategy
    toolkit_revision: str
    lock_hash: str
    scenario_events: tuple[ScenarioEvent, ...] = ()
    seed: int = 0


class Replay:
    """从已初始化的数据库打开日频回放, 运行配置从持久化记录恢复"""

    def __init__(self, path: Path, simulation_id: UUID) -> None:
        if not isinstance(simulation_id, UUID):
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "运行 ID 须为 UUID")
        self._path = path.resolve()
        self._simulation_id = simulation_id
        self._market = MarketDataRepository(self._path)
        with _connection(self._path) as db:
            row = db.execute(
                """
                SELECT s.data_version, s.start_session, s.end_session, s.initial_cash, s.currency,
                       s.execution_config, s.scenario_version, s.scenario_manifest,
                       t.strategy_id, t.name, t.code_version, t.config,
                       s.seed, s.toolkit_revision, s.lock_hash
                FROM simulations s JOIN strategies t USING (strategy_id)
                WHERE s.simulation_id = ?
                """,
                [simulation_id],
            ).fetchone()
        if row is None:
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "找不到指定回放运行")
        (
            self._version,
            start,
            end,
            self._initial_cash,
            self._currency,
            execution,
            scenario_version,
            events,
            self._strategy_id,
            name,
            code_version,
            strategy,
            seed,
            toolkit_revision,
            lock_hash,
        ) = row
        if scenario_version != _SCENARIO_VERSION or (name, code_version) not in (
            (_FIXED_ROUND_TRIP, _STRATEGY_VERSION),
            (_MOMENTUM_ROTATION, _STRATEGY_VERSION),
            (_MOMENTUM_ROTATION, _COMPOSITE_STRATEGY_VERSION),
        ):
            raise SimulatorError(ErrorCode.INCONSISTENT_STATE, "回放配置版本不受支持")
        strategy = json.loads(strategy)
        if name == _FIXED_ROUND_TRIP:
            self._strategy = FixedRoundTripStrategy(
                UUID(strategy["instrument_id"]), strategy["quantity"]
            )
        else:
            self._strategy = MomentumRotationStrategy(
                tuple(UUID(value) for value in strategy["instruments"]),
                strategy["n_periods"],
                strategy["rebalance_every"],
                strategy["quantity"],
                strategy["volatility_window"]
                if code_version == _COMPOSITE_STRATEGY_VERSION
                else None,
            )
        self._execution = ExecutionConfig(
            **{key: Decimal(value) for key, value in json.loads(execution).items()}
        )
        self._events = tuple(
            ScenarioEvent(
                event["scenario_sequence"],
                datetime.fromisoformat(event["effective_at"]),
                datetime.fromisoformat(event["available_at"]),
                event["action"],
                UUID(event["instrument_id"]) if event["instrument_id"] else None,
            )
            for event in json.loads(events)
        )
        self._sessions = self._market.sessions(self._version, start, end)
        self._spec = ReplaySpec(
            simulation_id=simulation_id,
            data_version=self._version,
            start_session=start,
            end_session=end,
            initial_cash=self._initial_cash,
            execution=self._execution,
            strategy=self._strategy,
            toolkit_revision=toolkit_revision,
            lock_hash=lock_hash,
            scenario_events=self._events,
            seed=seed,
        )

    @property
    def spec(self) -> ReplaySpec:
        """返回已保存的配置, 可更换 simulation_id 后用于重跑"""
        return self._spec

    @classmethod
    def create(cls, path: Path, spec: ReplaySpec) -> "Replay":
        """创建新运行, 区间须在冻结日历覆盖范围内且包含交易日

        币种沿用数据包, 调用方须确认区间无公司行动
        运行 ID 已存在时使用 Replay(path, id) 打开原运行
        """
        if (
            not isinstance(spec.simulation_id, UUID)
            or type(spec.start_session) is not date
            or type(spec.end_session) is not date
            or spec.start_session > spec.end_session
            or not isinstance(spec.execution, ExecutionConfig)
            or not isinstance(spec.scenario_events, tuple)
            or type(spec.seed) is not int
            or not -(2**63) <= spec.seed < 2**63
            or not all(
                isinstance(value, str) and value.strip()
                for value in (spec.data_version, spec.toolkit_revision, spec.lock_hash)
            )
        ):
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "回放配置无效")
        strategy = spec.strategy
        code_version = _STRATEGY_VERSION
        if type(strategy) is FixedRoundTripStrategy:
            if (
                not isinstance(strategy.instrument_id, UUID)
                or strategy.quantity >= 2**63
            ):
                raise SimulatorError(ErrorCode.INVALID_REQUEST, "固定策略配置无效")
            name = _FIXED_ROUND_TRIP
            instruments = (strategy.instrument_id,)
            config = _json(
                {
                    "instrument_id": str(strategy.instrument_id),
                    "quantity": strategy.quantity,
                }
            )
        elif type(strategy) is MomentumRotationStrategy:
            name = _MOMENTUM_ROTATION
            instruments = strategy.instruments
            params = {
                "instruments": [str(instrument) for instrument in instruments],
                "n_periods": strategy.n_periods,
                "rebalance_every": strategy.rebalance_every,
                "quantity": strategy.quantity,
            }
            if strategy.volatility_window is not None:
                params["volatility_window"] = strategy.volatility_window
                code_version = _COMPOSITE_STRATEGY_VERSION
            config = _json(params)
        else:
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "回放策略不受支持")
        validate_execution_config(spec.execution)
        _check_money((spec.execution.currency_unit,), ErrorCode.INVALID_REQUEST)
        initial = portfolio.initialize(
            spec.initial_cash, currency_unit=spec.execution.currency_unit
        )
        cash = initial.state.cash
        _check_money((cash,), ErrorCode.INVALID_REQUEST)
        calendar = MarketDataRepository(path).sessions(
            spec.data_version, date.min, date.max
        )
        sessions = tuple(
            day
            for day in calendar
            if spec.start_session <= day.session_date <= spec.end_session
        )
        if (
            not sessions
            or spec.start_session < calendar[0].session_date
            or spec.end_session > calendar[-1].session_date
        ):
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST, "回放区间超出冻结日历或无交易日"
            )
        first = SimulationClock(sessions).peek()
        assert first is not None
        ScenarioEventEngine.apply(first, ScenarioState(), spec.scenario_events)
        events = sorted(spec.scenario_events, key=lambda event: event.scenario_sequence)
        definition = sha256(_json([name, code_version, config]).encode()).hexdigest()
        strategy_id = uuid5(NAMESPACE_URL, f"quantsim:strategy:{definition}")

        with _connection(path, write=True) as db:
            if db.execute(
                "SELECT 1 FROM simulations WHERE simulation_id = ?",
                [spec.simulation_id],
            ).fetchone():
                raise SimulatorError(ErrorCode.INVALID_REQUEST, "回放运行 ID 已存在")
            (manifest,) = db.execute(
                "SELECT manifest FROM market_datasets WHERE data_version = ?",
                [spec.data_version],
            ).fetchone()
            _check_instruments(
                db,
                spec.data_version,
                set(instruments)
                | {
                    event.instrument_id
                    for event in events
                    if event.instrument_id is not None
                },
            )
            db.execute(
                """
                INSERT INTO strategies (strategy_id, name, code_version, config, definition_hash)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT (definition_hash) DO NOTHING
                """,
                [strategy_id, name, code_version, config, definition],
            )
            db.execute(
                """
                INSERT INTO simulations (
                    simulation_id, strategy_id, data_version, data_manifest,
                    scenario_version, scenario_manifest, start_session, end_session,
                    currency, initial_cash, execution_config, seed, toolkit_revision, lock_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    spec.simulation_id,
                    strategy_id,
                    spec.data_version,
                    manifest,
                    _SCENARIO_VERSION,
                    _json(
                        [
                            {
                                **asdict(event),
                                "instrument_id": str(event.instrument_id)
                                if event.instrument_id
                                else None,
                                "effective_at": event.effective_at.isoformat(),
                                "available_at": event.available_at.isoformat(),
                            }
                            for event in events
                        ]
                    ),
                    spec.start_session,
                    spec.end_session,
                    json.loads(manifest)["currency"],
                    str(cash),
                    _json(
                        {
                            key: str(value)
                            for key, value in asdict(spec.execution).items()
                        }
                    ),
                    spec.seed,
                    spec.toolkit_revision,
                    spec.lock_hash,
                ],
            )
            db.execute(
                "INSERT INTO cash_ledger VALUES (?, 0, 0, NULL, 'initial', ?, ?)",
                [spec.simulation_id, str(cash), first.market_close_at],
            )
            _insert_event(
                db,
                spec.simulation_id,
                0,
                1,
                "run_started",
                first,
                "replay",
                {"data_version": spec.data_version},
            )
        return cls(path, spec.simulation_id)

    def next_tick(self) -> Tick | None:
        """读取下一未提交 tick, 回放结束时返回 None"""
        with _connection(self._path) as db:
            committed = self._committed_seq(db)
        return SimulationClock(self._sessions, committed_seq=committed).peek()

    def performance(
        self, *, benchmark_instruments: tuple[UUID, ...] | None = None
    ) -> PerformanceResult:
        """统计从期初到最后已提交日的权益、费用、收益与基准

        基准默认使用策略证券池, 可传入非空且无重复的同批次 UUID 元组
        损坏或不连续的检查点返回 INCONSISTENT_STATE, 空运行返回空日序列
        """
        if benchmark_instruments is None:
            benchmark_instruments = (
                (self._strategy.instrument_id,)
                if isinstance(self._strategy, FixedRoundTripStrategy)
                else self._strategy.instruments
            )
        if (
            not isinstance(benchmark_instruments, tuple)
            or not benchmark_instruments
            or not all(isinstance(key, UUID) for key in benchmark_instruments)
            or len(set(benchmark_instruments)) != len(benchmark_instruments)
        ):
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST, "基准须为非空的唯一 UUID 元组"
            )
        instruments = tuple(sorted(benchmark_instruments))
        with _connection(self._path) as db:
            _check_instruments(db, self._version, set(instruments))
            rows = db.execute(
                """
                WITH fees AS (
                    SELECT tick_seq, sum(fee) AS amount
                    FROM fills WHERE simulation_id = ? GROUP BY tick_seq
                )
                SELECT e.tick_seq, e.payload, coalesce(f.amount, 0)
                FROM events e LEFT JOIN fees f USING (tick_seq)
                WHERE e.simulation_id = ? AND event_type = 'tick_committed' AND event_seq = 0
                ORDER BY e.tick_seq
                """,
                [self._simulation_id, self._simulation_id],
            ).fetchall()
        sessions = self._sessions[: len(rows)]
        equities = []
        fees = []
        if len(rows) > len(sessions):
            raise SimulatorError(ErrorCode.INCONSISTENT_STATE, "检查点超出回放范围")
        for expected, (seq, payload, fee) in enumerate(rows, start=1):
            if seq != expected:
                raise SimulatorError(ErrorCode.INCONSISTENT_STATE, "回放检查点不连续")
            _, equity = self._checkpoint_amounts(
                payload, sessions[seq - 1].session_date
            )
            equities.append(equity)
            fees.append(fee)
        return build_performance(
            simulation_id=self._simulation_id,
            version=self._version,
            currency=self._currency,
            initial_equity=self._initial_cash,
            market=self._market,
            sessions=sessions,
            equities=tuple(equities),
            fees=tuple(fees),
            benchmark_instruments=instruments,
            complete=len(sessions) == len(self._sessions),
        )

    def report(
        self,
        *,
        benchmark_instruments: tuple[UUID, ...] | None = None,
        output: Path | None = None,
    ) -> "Figure":
        """渲染已提交区间的净值和回撤, 调用方负责关闭返回的 Figure

        缺少连续策略收益时返回 DATA_UNAVAILABLE, output 可保存到已有目录
        """
        if output is not None and (
            not isinstance(output, Path) or output.resolve() == self._path
        ):
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "无效的报告输出路径")
        result = self.performance(benchmark_instruments=benchmark_instruments)
        return render_report(result, output=output, database_path=self._path)

    def record(
        self,
        *,
        benchmark_instruments: tuple[UUID, ...] | None = None,
        report_directory: Path | None = None,
    ) -> ReplayRecord:
        """保存非空已提交前缀的绩效, 可在已有目录发布 PNG 后一起登记

        同一前缀及基准只有一条记录, 仅保存绩效时保留原记录
        报告生成失败时保留账本及原记录, 可从检查点再次调用
        """
        if report_directory is not None and not isinstance(report_directory, Path):
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "无效的报告目录")
        result = self.performance(benchmark_instruments=benchmark_instruments)
        if not result.days:
            raise SimulatorError(ErrorCode.DATA_UNAVAILABLE, "运行尚无已提交的交易日")
        output, digest = (
            publish_report(result, report_directory.resolve())
            if report_directory is not None
            else (None, None)
        )
        key = (
            self._simulation_id,
            len(result.days),
            _json([str(key) for key in result.benchmark_instruments]),
        )
        metrics = _json(
            {
                "total_fees": str(result.total_fees),
                "portfolio": asdict(result.portfolio),
                "benchmark": asdict(result.benchmark),
                "benchmark_missing_sessions": [
                    day.isoformat() for day in result.benchmark_missing_sessions
                ],
            }
        )
        with _connection(self._path, write=True) as db:
            db.execute(
                """
                INSERT INTO replay_results (
                    simulation_id, tick_seq, benchmark_instruments, metrics,
                    report_path, report_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (simulation_id, tick_seq, benchmark_instruments)
                DO UPDATE SET metrics = excluded.metrics, report_path = excluded.report_path,
                              report_sha256 = excluded.report_sha256
                WHERE excluded.report_path IS NOT NULL
                """,
                [*key, metrics, str(output) if output is not None else None, digest],
            )
            row = db.execute(
                """
                SELECT tick_seq, benchmark_instruments, metrics, report_path, report_sha256
                FROM replay_results
                WHERE simulation_id = ? AND tick_seq = ? AND benchmark_instruments = ?
                """,
                key,
            ).fetchone()
            assert row is not None
        return self._read_record(row)

    def records(self) -> tuple[ReplayRecord, ...]:
        """按 tick 和基准证券池读取已保存的绩效, 无需重算或打开报告文件"""
        with _connection(self._path) as db:
            rows = db.execute(
                """
                SELECT tick_seq, benchmark_instruments, metrics, report_path, report_sha256
                FROM replay_results WHERE simulation_id = ?
                ORDER BY tick_seq, benchmark_instruments
                """,
                [self._simulation_id],
            ).fetchall()
        return tuple(self._read_record(row) for row in rows)

    def _read_record(
        self, row: tuple[int, str, str, str | None, str | None]
    ) -> ReplayRecord:
        seq, instruments, payload, path, digest = row
        if seq > len(self._sessions):
            raise SimulatorError(ErrorCode.INCONSISTENT_STATE, "绩效区间超出回放范围")
        metrics = json.loads(payload)
        return ReplayRecord(
            simulation_id=self._simulation_id,
            tick_seq=seq,
            end_session=self._sessions[seq - 1].session_date,
            complete=seq == len(self._sessions),
            benchmark_instruments=tuple(UUID(key) for key in json.loads(instruments)),
            benchmark_missing_sessions=tuple(
                date.fromisoformat(day) for day in metrics["benchmark_missing_sessions"]
            ),
            currency=self._currency,
            total_fees=Decimal(metrics["total_fees"]),
            portfolio=PerformanceMetrics(**metrics["portfolio"]),
            benchmark=PerformanceMetrics(**metrics["benchmark"]),
            report_path=Path(path) if path is not None else None,
            report_sha256=digest,
        )

    def step(self, tick_seq: int) -> PortfolioSnapshot:
        """提交指定的下一 tick, 重试已提交的序号返回持久化快照

        跳步或无效序号返回 INVALID_REQUEST, 数据库写入失败可重试同一序号
        """
        if type(tick_seq) is not int or not 1 <= tick_seq <= len(self._sessions):
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "tick 序号超出回放区间")
        with _connection(self._path) as db:
            committed = self._committed_seq(db)
            if tick_seq <= committed:
                return self._snapshot(db, tick_seq)
            if tick_seq != committed + 1:
                raise SimulatorError(ErrorCode.INVALID_REQUEST, "不能跳过未提交 tick")
            orders = tuple(
                Order(*row)
                for row in db.execute(
                    """
                    SELECT order_id, instrument_id, side, quantity, submitted_tick,
                           target_tick, intent_seq
                    FROM orders WHERE simulation_id = ? AND target_tick = ?
                    ORDER BY intent_seq
                    """,
                    [self._simulation_id, tick_seq],
                ).fetchall()
            )
            previous = self._snapshot(db, committed) if committed else None

        clock = SimulationClock(self._sessions, committed_seq=committed)
        tick = clock.peek()
        assert tick is not None
        market = self._market.view(self._version, tick.session_date)
        account = (
            PortfolioState(
                previous.cash, {p.instrument_id: p.quantity for p in previous.positions}
            )
            if previous
            else PortfolioState(self._initial_cash, {})
        )
        limits, pending = ScenarioState(), self._events
        if previous:
            restored = ScenarioEventEngine.apply(previous.tick, limits, pending)
            limits, pending = restored.state, restored.pending
        scenario = ScenarioEventEngine.apply(tick, limits, pending)
        execution = execute(
            tick, orders, market, scenario.state, account, self._execution
        )
        fills = tuple(outcome.fill for outcome in execution.outcomes if outcome.fill)
        for order, outcome in zip(orders, execution.outcomes, strict=True):
            fill = outcome.fill
            if outcome.order_id != order.order_id or (
                fill is not None
                and (
                    (fill.order_id, fill.instrument_id, fill.side, fill.quantity)
                    != (order.order_id, order.instrument_id, order.side, order.quantity)
                    or (fill.data_version, fill.session_date, fill.executed_at)
                    != (self._version, tick.session_date, tick.market_close_at)
                    or order.target_tick != tick.seq
                )
            ):
                raise SimulatorError(
                    ErrorCode.INCONSISTENT_STATE, "成交与到期订单或 tick 不一致"
                )
        booked = portfolio.apply_fills(account, fills)
        if booked.state != PortfolioState(
            execution.cash,
            {key: qty for key, qty in execution.positions.items() if qty},
        ):
            raise SimulatorError(
                ErrorCode.INCONSISTENT_STATE, "过账结果与撮合状态不一致"
            )
        snapshot = portfolio.mark(
            tick, booked.state, market, currency_unit=self._execution.currency_unit
        )
        _check_money(
            (
                snapshot.cash,
                snapshot.equity,
                *(p.market_value for p in snapshot.positions),
                *(value for fill in fills for value in (fill.notional, fill.fee)),
                *(entry.amount for entry in booked.entries),
            ),
            ErrorCode.INCONSISTENT_STATE,
        )
        finished = tick.seq == len(self._sessions)
        intents = (
            ()
            if finished
            else self._strategy.decide(
                StrategyContext(
                    tick,
                    market,
                    MappingProxyType(booked.state.positions),
                    scenario.state,
                )
            )
        )
        if any(
            not isinstance(intent, OrderIntent)
            or not isinstance(intent.instrument_id, UUID)
            or intent.side not in ("buy", "sell")
            or type(intent.quantity) is not int
            or not 0 < intent.quantity < 2**63
            for intent in intents
        ):
            raise SimulatorError(ErrorCode.INVALID_REQUEST, "策略订单意图无效")
        next_orders = tuple(
            Order(
                uuid5(self._simulation_id, f"order:{tick.seq}:{index}"),
                intent.instrument_id,
                intent.side,
                intent.quantity,
                tick.seq,
                tick.seq + 1,
                index,
            )
            for index, intent in enumerate(intents)
        )

        with _connection(self._path, write=True) as db:
            current = self._committed_seq(db)
            if tick_seq <= current:
                return self._snapshot(db, tick_seq)
            if current != committed:
                raise SimulatorError(
                    ErrorCode.INCONSISTENT_STATE, "提交前回放检查点发生变动"
                )
            _check_instruments(
                db, self._version, {order.instrument_id for order in next_orders}
            )
            self._write_tick(
                db,
                snapshot,
                scenario.applied,
                orders,
                execution.outcomes,
                booked,
                next_orders,
                finished,
            )
        return snapshot

    def _committed_seq(self, db: duckdb.DuckDBPyConnection) -> int:
        (seq,) = db.execute(
            """
            SELECT coalesce(max(tick_seq), 0) FROM events
            WHERE simulation_id = ? AND event_type = 'tick_committed' AND event_seq = 0
            """,
            [self._simulation_id],
        ).fetchone()
        return seq

    def _snapshot(self, db: duckdb.DuckDBPyConnection, seq: int) -> PortfolioSnapshot:
        row = db.execute(
            """
            SELECT payload FROM events WHERE simulation_id = ? AND tick_seq = ?
            AND event_seq = 0 AND event_type = 'tick_committed'
            """,
            [self._simulation_id, seq],
        ).fetchone()
        if row is None:
            raise SimulatorError(
                ErrorCode.INCONSISTENT_STATE, f"缺少 tick {seq} 检查点"
            )
        day = self._sessions[seq - 1]
        tick = Tick(day.session_date, day.market_close_at, day.cutoff_at, seq)
        cash, equity = self._checkpoint_amounts(row[0], tick.session_date)
        positions = tuple(
            PositionMark(*position)
            for position in db.execute(
                """
                SELECT p.instrument_id, p.quantity, p.mark_session, p.mark_price,
                       p.market_value, b.source_ref, p.stale
                FROM positions_daily p JOIN market_bars b
                  ON (p.data_version, p.instrument_id, p.mark_session)
                   = (b.data_version, b.instrument_id, b.session_date)
                WHERE p.simulation_id = ? AND p.tick_seq = ? ORDER BY p.instrument_id
                """,
                [self._simulation_id, seq],
            ).fetchall()
        )
        return PortfolioSnapshot(
            tick,
            self._version,
            cash,
            positions,
            equity,
        )

    def _checkpoint_amounts(
        self, payload: str, session_date: date
    ) -> tuple[Decimal, Decimal]:
        try:
            checkpoint = json.loads(payload)
            if (checkpoint["session_date"], checkpoint["data_version"]) != (
                session_date.isoformat(),
                self._version,
            ):
                raise ValueError("检查点的日期或行情版本不一致")
            cash, equity = Decimal(checkpoint["cash"]), Decimal(checkpoint["equity"])
            if not (cash.is_finite() and equity.is_finite() and 0 <= cash <= equity):
                raise ValueError("检查点金额不一致")
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise SimulatorError(
                ErrorCode.INCONSISTENT_STATE, "回放检查点无效"
            ) from exc
        return cash, equity

    def _write_tick(
        self,
        db: duckdb.DuckDBPyConnection,
        snapshot: PortfolioSnapshot,
        events: tuple[ScenarioEvent, ...],
        orders: tuple[Order, ...],
        outcomes: tuple[OrderOutcome, ...],
        booked: BookingResult,
        next_orders: tuple[Order, ...],
        finished: bool,
    ) -> None:
        run, tick = self._simulation_id, snapshot.tick
        event_seq = 1
        for event in events:
            _insert_event(
                db,
                run,
                tick.seq,
                event_seq,
                "scenario_applied",
                tick,
                f"scenario:{event.scenario_sequence}",
                {"scenario_sequence": event.scenario_sequence, "action": event.action},
                instrument_id=event.instrument_id,
                times=(event.effective_at, event.available_at),
            )
            event_seq += 1
        entries = iter(booked.entries)
        for order, outcome in zip(orders, outcomes, strict=True):
            fill = outcome.fill
            source = "execution"
            if fill:
                source = fill.bar_source_ref
                fill_id = uuid5(run, f"fill:{order.order_id}")
                db.execute(
                    """
                    INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        fill_id,
                        run,
                        order.order_id,
                        fill.instrument_id,
                        fill.data_version,
                        fill.session_date,
                        tick.seq,
                        fill.executed_at,
                        fill.quantity,
                        fill.price,
                        fill.price,
                        str(fill.notional),
                        str(fill.fee),
                    ],
                )
                entry = next(entries)
                db.execute(
                    "INSERT INTO cash_ledger VALUES (?, ?, ?, ?, 'trade', ?, ?)",
                    [
                        run,
                        tick.seq,
                        order.intent_seq,
                        fill_id,
                        str(entry.amount),
                        tick.market_close_at,
                    ],
                )
            _insert_event(
                db,
                run,
                tick.seq,
                event_seq,
                "order_closed",
                tick,
                source,
                {"reason": outcome.reason},
                order_id=order.order_id,
                instrument_id=order.instrument_id,
            )
            event_seq += 1
        for position in snapshot.positions:
            db.execute(
                "INSERT INTO positions_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run,
                    tick.session_date,
                    position.instrument_id,
                    tick.seq,
                    self._version,
                    position.mark_session,
                    position.quantity,
                    position.mark_price,
                    str(position.market_value),
                    position.stale,
                    tick.market_close_at,
                ],
            )
        for order in next_orders:
            db.execute(
                """
                INSERT INTO orders VALUES (?, ?, ?, ?, 'strategy', ?, ?, ?, ?, ?, ?)
                """,
                [
                    order.order_id,
                    run,
                    self._strategy_id,
                    order.instrument_id,
                    order.side,
                    order.quantity,
                    order.submitted_tick,
                    tick.cutoff_at,
                    order.intent_seq,
                    order.target_tick,
                ],
            )
            _insert_event(
                db,
                run,
                tick.seq,
                event_seq,
                "order_submitted",
                tick,
                "strategy",
                {"target_tick": order.target_tick},
                order_id=order.order_id,
                instrument_id=order.instrument_id,
                times=(tick.cutoff_at, tick.cutoff_at),
            )
            event_seq += 1
        if finished:
            _insert_event(
                db, run, tick.seq, event_seq, "run_finished", tick, "replay", {}
            )
        _insert_event(
            db,
            run,
            tick.seq,
            0,
            "tick_committed",
            tick,
            "replay",
            {
                "session_date": tick.session_date.isoformat(),
                "data_version": self._version,
                "cash": str(snapshot.cash),
                "equity": str(snapshot.equity),
            },
        )


@contextmanager
def _connection(
    path: Path, *, write: bool = False
) -> Iterator[duckdb.DuckDBPyConnection]:
    try:
        with duckdb.connect(path, read_only=not write) as db:
            db.begin()
            yield db
            db.commit()
    except duckdb.Error as exc:
        raise SimulatorError(ErrorCode.DATA_UNAVAILABLE, "回放数据库读写失败") from exc


def _check_instruments(
    db: duckdb.DuckDBPyConnection, version: str, instruments: set[UUID]
) -> None:
    if not instruments:
        return
    rows = db.execute(
        "SELECT instrument_id FROM market_listings WHERE data_version = ? AND instrument_id IN (SELECT unnest(?))",
        [version, sorted(instruments)],
    ).fetchall()
    if {row[0] for row in rows} != instruments:
        raise SimulatorError(
            ErrorCode.INVALID_REQUEST, "场景或订单证券不属于冻结行情版本"
        )


def _check_money(values: tuple[Decimal, ...], code: ErrorCode) -> None:
    with account_context():
        if any(
            abs(value) >= 10**16 or value != value.quantize(Decimal("0.00000001"))
            for value in values
        ):
            raise SimulatorError(
                code, "账户金额或货币单位超出数据库 DECIMAL(24,8) 精度"
            )


def _insert_event(
    db: duckdb.DuckDBPyConnection,
    run: UUID,
    tick_seq: int,
    event_seq: int,
    event_type: str,
    tick: Tick,
    source: str,
    payload: dict,
    *,
    order_id: UUID | None = None,
    instrument_id: UUID | None = None,
    times: tuple[datetime, datetime] | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO events (
            simulation_id, tick_seq, event_seq, event_type, order_id, instrument_id,
            effective_at, available_at, source_ref, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run,
            tick_seq,
            event_seq,
            event_type,
            order_id,
            instrument_id,
            *(times or (tick.market_close_at, tick.cutoff_at)),
            source,
            _json(payload),
        ],
    )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
