# QuantSim 接口

本文记录当前接口及后续回放模块的接口草案。领域对象由 Python 类型描述，测试从同一接口调用。时间和来源上下文由协调器显式传入。下文的 `as_of` 等于当前 tick 的 `cutoff_at`。

## 共享数据形状

| 类型 | 字段及约束 |
| --- | --- |
| `ReplaySpec` | `simulation_id`、冻结 `data_version`、场景及策略版本、回放日期区间、初始权益、币种、执行参数、种子 |
| `Session` / `Tick` | `session_date: date` 为交易所当地交易日，`market_close_at`、`cutoff_at` 为带时区 UTC 时刻。Tick 加 `seq: int`，从 1 开始 |
| `Bar` / `BarBatch` | 证券 ID、交易日、`bar_end_at`、`available_at`、原始 OHLC、成交量、可交易状态及来源。批次加 `data_version`、`as_of` |
| `MarketView` | 按当前 `as_of` 截断的历史查询对象。只暴露有效证券和过去已发布的数据，包括当日已发布 bar |
| `OrderIntent` / `Order` | 意图含证券 ID、方向与正整数股数。接受后加订单 ID、策略 ID、提交 tick 与目标 tick。订单顺序按当日意图下标固定 |
| `Fill` / `ExecutionResult` | 成交含订单 ID、参考价、成交价、股数、费用、成交时刻和 bar 引用。结果还含按订单顺序输出的结束原因，初期每单至多一笔成交 |
| `PortfolioState` / `PortfolioSnapshot` | 状态含现金和各证券股数。快照再含逐证券标记价、行情引用、stale、日终权益和 tick |
| `ScenarioState` | 已生效的市场或证券限制。首版事件为 halt / resume，可冻结撮合直到恢复 |
| `Capability` | 工具箱全名、公开契约、Simulator 用途与是否启用。面向策略的调用仅显示启用的策略能力 |

账本价和金额用 Decimal。研究面板与统计由 Adapter 转为 pandas 数值。运行期间同一证券 ID 映射到唯一 Toolkit ticker，市场/证券代码不直接充当持久化主键。

## MarketDataRepository

```python
def import_batch(source: Path) -> DatasetRef: ...
def sessions(data_version: str, start: date, end: date) -> tuple[Session, ...]: ...
def history(
    data_version: str,
    instruments: tuple[UUID, ...],
    *,
    start: date,
    end: date,
    as_of: datetime,
) -> BarBatch: ...
def session_bars(
    data_version: str, instruments: tuple[UUID, ...], *, tick: Tick
) -> BarBatch: ...
```

`import_batch` 校验日历、市场、币种、唯一键和时区，按内容锁定版本。输入为当前历史市场格式的本地 JSON 文件。相同批次重复导入返回同一版本，内容变更得到新版本。读接口同时要求 `bar_end_at <= as_of`、`available_at <= as_of`。历史查询范围按当地交易日闭区间，交易日来自清单。`session_bars` 只给当期已发布真实 bar，缺失证券不生成伪行。数据库失败返回 `DATA_UNAVAILABLE`，合法缺行用空结果表达。

### 分钟行情预留

分钟行情接入时，在 `MarketDataRepository` 增加按 bar 结束时刻查询的接口。当前交付为接口约定，现有 `history`、`session_bars` 和快照继续服务日频调用。

```python
def bar_history(
    data_version: str,
    instruments: tuple[UUID, ...],
    *,
    interval: Literal["1m", "1d"],
    after: datetime,
    through: datetime,
    as_of: datetime,
) -> BarBatch: ...
```

- `after`、`through` 和 `as_of` 须带时区，统一转换成 UTC。按 `after < bar_end_at <= through` 选取窗口，同时要求 `bar_end_at <= as_of` 和 `available_at <= as_of`。例如一根 09:31 结束、09:31:03 发布的 bar，在 09:31:02 的视图里是缺行，在 09:31:03 才可见。策略入口由回放视图绑定 `as_of`。
- 复用 `Bar` 的 OHLCV、可用时刻和来源。当地 `session_date` 用于归属交易日。分钟行情按 `(data_version, instrument_id, interval, bar_end_at)` 唯一，查询结果按结束时刻和证券键稳定排序。每次查询只取明确指定的粒度。版本不含该粒度时应报错，指定粒度下的合法缺行返回空结果或保留在序列间隙。
- 首个分钟数据包需要给出交易时段和 bar 的结束时刻，覆盖午休和提前收盘等边界。分钟特征若接入 Toolkit，面板索引须保留完整的 bar 结束时刻，窗口按分钟观察定义。现有日频特征入口继续使用交易日索引。

## SimulationClock

```python
class SimulationClock:
    def __init__(
        self, sessions: tuple[Session, ...], *, committed_seq: int = 0
    ) -> None: ...
    def peek(self) -> Tick | None: ...
    def advance(self, committed_seq: int) -> None: ...
```

用 `market.sessions(data_version, start, end)` 取得冻结版本在回放区间内的交易日历，再构造 [SimulationClock](../src/quantsim/simulation_clock.py)。交易日、UTC 收盘时刻和 UTC cutoff 各自严格递增，且 cutoff 不早于对应收盘。序号从区间首个交易日的 1 开始，假期和周末由日历决定。

`peek()` 重复读取返回同一个待提交 tick，空区间或最后一日提交后返回 `None`。协调器完成持久化后调用 `advance(tick.seq)`。跳号、重复、回退及结束后推进会抛出 `INCONSISTENT_STATE`，时钟位置保持原值。恢复时用同一版本和日期区间构造时钟，并传入最后一个已提交序号。`committed_seq=0` 表示尚未提交，超过日历长度或非法日历会抛出 `INVALID_REQUEST`。

## ScenarioEventEngine

```python
def apply(
    tick: Tick, state: ScenarioState, pending: tuple[ScenarioEvent, ...]
) -> ScenarioUpdate: ...
```

[ScenarioEventEngine](../src/quantsim/scenario.py) 只消费 `effective_at <= tick.market_close_at` 且 `available_at <= tick.cutoff_at` 的未应用事件，按 `(effective_at, scenario_sequence)` 排序。事件使用 UTC，序号在场景内唯一。同一对象的可用时间须随执行顺序单调，避免迟到的旧事件覆盖更新状态。`instrument_id=None` 表示全市场，市场和单只证券的停牌状态独立。返回更新后的限制、已应用事件和仍待处理的事件。协调器在撮合前应用事件，只把结果状态交给策略。

## Strategy

```python
def decide(context: StrategyContext) -> tuple[OrderIntent, ...]: ...
```

`StrategyContext` 含当前 tick、MarketView、最新 PortfolioSnapshot、可见 ScenarioState 及策略专用 QuantToolkitAdapter 接口。初期策略状态可由输入、配置与持仓完全重建。历史不足返回空元组，意图顺序稳定。策略只能得到截至此刻已发布的特征。目标权重是研究产物，未来接入时由策略层显式决定其订单换算、空头约束和遗漏证券处理。

当前的 [FixedRoundTripStrategy](../src/quantsim/strategy.py) 用于三日验收。它接收 tick、绑定该 tick 的市场视图、当前股数和交易限制。证券当前可见且未受场景限制时，第一日从空仓提交定量买入，第二日按实际持仓提交卖出。策略输出订单意图，由撮合环节决定实际成交。

## ExecutionEngine

```python
def execute(
    tick: Tick,
    orders: tuple[Order, ...],
    bars: BarBatch,
    limits: ScenarioState,
    state: PortfolioState,
    config: ExecutionConfig,
) -> ExecutionResult: ...
```

只处理 `target_tick == tick.seq` 的有效订单，按 `(submitted_tick, intent_seq)` 排序。必须有真实、可交易、正量且当时可见的当期 bar。按证券的成交容量为 `floor(volume * participation_rate)`，在本 tick 各单间扣减。首版滑点为 0，参考价与成交价为同一原始 close。成交额和费用都按账户最小货币单位进行半偶舍入，费用由未舍入成交额乘配置费率得到。逐单扣减可用容量和现金或可卖量，卖出款在模拟账户即时可用。无 bar、停牌、容量不足、现金不足和可卖量不足分别记录结束原因。不会跨 tick 自动重试，初期省略部分成交和其他委托类型。结果是待提交的成交事实及状态变动，数据库失败时可重算。

## PortfolioManager

```python
def apply_fills(state: PortfolioState, fills: tuple[Fill, ...]) -> BookingResult: ...
def mark(tick: Tick, state: PortfolioState, marks: MarkSet) -> PortfolioSnapshot: ...
```

初始现金是显式流水。按成交顺序形成有符号的交易净流水，买入为 `-(成交额 + 费用)`，卖出为 `成交额 - 费用`。每笔流水和持仓使用同一成交金额口径。重复成交 ID、未知证券或超出只做多/现金限制属于状态不一致，回放步骤回滚。估值采用当期原始 close 或上一个已知真实价并带 stale，记录引用价格的日期。权益为现金加持仓市值。组合收益入口接受由日终权益和期初资金推导的序列，验证期序和权益。初期无外部入金和出金，仅初始化账户。

## QuantToolkitAdapter

```python
def capabilities(
    *, stage: str | None = None, workflow: str | None = None
) -> tuple[Capability, ...]: ...
def describe(name: str) -> Capability: ...
def for_strategy(*, market: MarketView) -> StrategyAnalytics: ...
def performance(history: CompletedEquityHistory) -> PerformanceResult: ...
def report(
    kind: ReportKind, artifacts: ReportArtifacts, *, output: Path | None = None
) -> Figure: ...
```

策略入口初版为 `StrategyAnalytics.feature(name, instruments, *, lookback_sessions, params) -> FeatureResult`。由绑定的 MarketView 取数，`params` 只允许该能力的标量选项。通过内部固定绑定调用对应公开函数，窗口或偏移先校验整数域。首版 fixture 显式绑定 `price_col="close"`，以原始收盘价计算。采用调整价格时，批次须证明该价格在 `as_of` 可得。策略得到不可修改的领域结果，窗口、证券、批次、`as_of` 和列口径可追溯。完整 Toolkit 查询清单仅用于检查和显示。标签、训练、写文件、联网和私有函数不暴露给策略。

内部按语义使用已登记 pandas 对象，不通过 `__wrapped__` 跳过入口。不同调用通道可复用同一份映射和转接实现。离线模型与归因接口按 [implementation_plan.md](implementation_plan.md) 引入。回测收益报告复用 `plot_report("event_driven", ...)`。只有验证了连续权益序列的结果可作为组合收益报告输入。调用者负责关闭返回的 Figure。

| 错误码 | 使用条件 |
| --- | --- |
| `UNKNOWN_CAPABILITY` / `CAPABILITY_DISABLED` | 目录无条目或当前通道未启用 |
| `INVALID_REQUEST` | Simulator 参数绑定、窗口范围、类型或值域失败 |
| `INVALID_LINEAGE` / `DATA_NOT_VISIBLE` | Simulator 发现批次/经济类型不匹配或时点越界 |
| `TOOLKIT_INPUT_REJECTED` | 工具箱公开入口拒绝输入，包括已知校验的 TypeError、ValueError、KeyError |
| `DEPENDENCY_UNAVAILABLE` / `DATA_UNAVAILABLE` | 已识别的导入、原生依赖、读取或写入失败 |
| `TOOLKIT_FAILURE` / `INCONSISTENT_STATE` | 其余工具箱计算故障或回放状态不一致 |

错误包含 code、能力名或模块、simulation_id、tick_seq 和简短上下文，保留原始异常链。已知可恢复的订单拒绝通过 `ExecutionResult` 记录。能力调用失败由协调器停止当前 tick 并回滚，使用已提交状态恢复。


## 当前可用接口

当前提供 [初始化](../src/quantsim/database.py)、[行情仓库](../src/quantsim/market_data.py) 与 [Toolkit Adapter](../src/quantsim/toolkit_adapter.py)。`import_batch` 接收冻结 JSON 文件的 Path。`market.view(data_version, session_date)` 从清单固定本次查询的交易日和 cutoff。

`historical_return` 是当前启用的策略特征。`params` 为映射，可包含 `n_periods`，默认 5，范围为 1 至 2³¹−1。Adapter 选择原始 `close`。当窗口大于可见日期数时，输出均为预热缺值，传给 Toolkit 的窗口限制在可见日期数以内以减少分配。结果保留请求证券、有效观察、截止时点及批次 ID。无可见行情时结果仍保留请求证券。

`capabilities` 与 `describe` 可查看 Toolkit 目录。当前计算入口为 `for_strategy` 所提供的特征。其余设计接口按实施计划加入。
