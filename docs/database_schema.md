# DuckDB 数据库设计

QuantSim 使用独立本地数据库。初期由单个回放进程串行写入，导入与回放写操作互斥。行情与元数据按批次冻结。运行头、订单、成交和账本追加写入，状态从检查点及事件派生。当前数据库以 [schema.sql](../src/quantsim/schema.sql) 为准。回放表的写入规则随对应里程碑验收。

## 时间、精度与数据来源

- `session_date DATE` 表示交易所当地交易日。`*_at TIMESTAMPTZ` 存储实际时刻，连接按 UTC 读写，界面按交易所时区显示。交易所日历独立保存时区与每个 session 的 close、cutoff，包含节假日与提前收盘。运行中的排序以 tick 和顺序号为准。真实创建时间与模拟时刻分字段保存。 [DuckDB 时间类型](https://duckdb.org/docs/current/sql/data_types/timestamp)
- 原始 OHLC、成交价和标记价采用 `DECIMAL(20,8)`，整数股与成交量用 `BIGINT`。账本用 `DECIMAL(24,8)`，账户的最小货币单位写入运行配置，落库前按账户规则舍入。成交额先以 Decimal 计算并舍入至最小货币单位。费用以未舍入成交额乘费率后独立舍入。逐证券市值按同一精度舍入，余额与权益以舍入后的值加总。研究收益和指标独立使用浮点数。
- `data_version` 是冻结导入清单的内容摘要，清单覆盖数据文件哈希、提供者、导入规则版本、单市场日历、证券映射和价格口径。清单与原始文件字节保存在 `market_datasets`，和证券、时点元数据及行情在同一个事务写入。创建运行时将已验证清单复制到 `simulations.data_manifest`，并检查对应批次、日历覆盖和币种。数据纠正产生新版本。行情行的 `source_ref` 指向具体导入对象或 fixture 记录。行情表首版只接受真实观测，研究补齐只在派生视图进行。
- 交易 ID 为 UUID，回放派生 ID 由运行 ID 和稳定业务键生成。同一运行的 retry 保持 ID。复合外键约束运行、订单、证券及行情的一致性。事件源 ID 放入 `source_ref`，场景原始参数和每次实际生效的序号分别保留。

| 表 | 来源与用途 |
| --- | --- |
| `instruments` | Fixture 或受控导入的证券身份，含市场、代码、币种、交易所时区和来源，主键独立于 ticker |
| `market_bars` | 冻结批次的观察记录，保存原始 OHLC、可用时刻、导入时刻及可交易标记 |
| `simulations` | 启动请求及冻结运行配置，记录策略、数据、场景与 Toolkit 版本 |
| `events` | 场景应用、订单终态、tick 提交与运行终态，由协调器在回放时写入。场景原始输入由运行中的版本化 manifest 保存 |
| `strategies` | 本地策略代码及配置的冻结版本，实验和模拟均引用策略 ID |
| `orders` | 用户或策略的已接受订单，由协调器分配目标 tick 和固定顺序 |
| `fills` | ExecutionEngine 产出的模拟成交，保留实际使用的 bar、参考价及费用 |
| `cash_ledger` | PortfolioManager 产出的初始资金与净成交现金流水 |
| `positions_daily` | PortfolioManager 的日终非零持仓，含标记价来源与 stale 状态 |
| `experiments` | 用户定义的研究问题、策略及固定配置 |
| `backtest_runs` | 离线回测结果，关联实验及可选的事件回放运行。记录时间区间、依赖、数据、参数、结果和产物位置 |

## 初始化与历史市场表

首次初始化在同一事务建立当前 schema，并在 `schema_info` 写入版本。重复启动核对表集和版本。开发阶段遇到不匹配的数据库时，使用新路径初始化并重新导入原始数据包。

| 表 | 记录与键 |
| --- | --- |
| `market_datasets` | 版本主键、清单 JSON、原始文件字节及导入时刻 |
| `market_listings` | 版本和证券为主键，上市及退市的有效日和消息可用时刻 |
| `market_symbols` | 版本、证券、代码变更有效日为主键，另有可用时刻 |
| `market_pool_changes` | 版本、池名、证券、变更有效日为主键，另有可用时刻 |
| `market_session_status` | 版本、证券、交易日为主键，独立保存已知停牌和无交易记录 |
| `market_adjusted_prices` | 版本、证券、交易日、可用时刻为主键，保留调整方法和修订 |
| `market_fundamentals` | 版本、证券、指标、报告期末、披露及可用时刻为主键，保留精确数值与单位 |

原始行情及辅助事实引用同版本的证券上市记录，并保留归档记录定位。原始行情按版本、证券和交易日组成主键。日期用 `DATE`，时刻用 `TIMESTAMPTZ`。版本化日历保存在 `market_datasets.manifest`，详情见 [历史市场](historical_market.md)。

## 索引、事务与校验

- 每表的 PK、UNIQUE、FK 使用 DuckDB 的约束索引。首版无手工 ART 索引。行情导入按 `(data_version, instrument_id, session_date)` 批内排序，让读取证券日期范围时可利用 zonemap。回放按 `simulation_id` 和 tick 范围读取，实际查询要用 `ORDER BY`。初期数据量增长后先看查询计划，再考虑单列高选择性索引。 [DuckDB 约束](https://duckdb.org/docs/current/sql/constraints) [DuckDB 索引](https://duckdb.org/docs/current/sql/indexes) [索引选择](https://duckdb.org/docs/guides/performance/indexing)
- 建立运行时，在同一事务写 `simulations`、tick 0 的初始现金流水和启动事件。每次 tick 在一个事务中写下一 tick 的订单、当期成交、现金、持仓、事件与最后一条 `tick_committed`。检查点约定 `event_seq=0`，业务事件序号从 1 起。检查点最后插入，payload 保存 session、权益、现金和版本引用，金额用精确十进制文本表示。按业务事件排序后应用检查点。空仓日仍有权益快照。
- 状态从最新完整检查点、相关持仓和事件派生。运行结束事件跟最后一个检查点同事务写入，失败诊断在回滚后独立记录，重试以已提交检查点判断。相同内容重试可确认已有提交。遇到相同业务键而内容不一致则停止。未成交订单从提交事件和终态推导。结果表按终态插入，外部图表文件按内容摘要保存，只有成功入库的结果引用有效产物。
- 应用层校验 DDL 无法表达的关系，包括 `fill.tick_seq = order.target_tick`，完整成交数量与订单一致，同一 tick 中成交 bar 已可见，正量、场景可交易，账本金额与成交方向及费用一致。持仓快照须与当日现金和所有已提交成交守恒。实验与模拟关联还须核对日期区间、币种、版本和冻结参数。
- 保留历史运行供复现，冻结表采用追加策略。迁移、重建或清理使用显式维护流程与备份。阶段推进及验证见 [implementation_plan.md](implementation_plan.md)。

## Phase 0 验证

在本机 DuckDB 1.5.6 内存库执行当时的设计 DDL，11 张表均创建成功。以固定单笔买入走通订单、成交、现金、持仓、事件检查点和两种回测结果模式。事务内插入不存在订单的事件触发外键拒绝，回滚后本 tick 的成交、流水、持仓和事件均撤销。用同一 ID 重试提交后，期初现金 100、成交 10、费用 0.10，余额 89.90、持仓市值 10。此验证覆盖 DDL 和提交行为，回放业务测试留到相应里程碑。
