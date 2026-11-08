# QuantSim 独立验收 · M4-01 波动率与合成调仓

## 验收对象

当前对象为 HEAD `ab3c7048d0465eee812d67343022bb63f6cf1e48` 加工作区改动。开发方实测 98 项 pytest 通过，Ruff 代码与格式检查通过，三日回放清仓权益 1,009.77 USD。

DeepSeek 独立复验通过。实测 98 项 pytest 通过，其中 Adapter 与动量回放共 28 项，四个独立复现脚本均通过。报告摘要见 [开发指南](../development_guide.md#m4-策略与绩效)。

| 文件 | SHA-256 |
| --- | --- |
| `src/quantsim/toolkit_adapter.py` | `abca793524e45cdb696cc7dc41106fcb52bca98014e9cf2dad1688b5d966a829` |
| `src/quantsim/strategy.py` | `9920971d639984afd8a1881656c246d9dcecf0bca851fcde49c7dad023f06e0b` |
| `src/quantsim/replay.py` | `85fabf0f0aa28fb8e475d5a8651e2dce290f2049ada203ebbf74d63a960004a5` |
| `tests/test_toolkit_adapter.py` | `cf19b9be50ed4aaf193f19bdf44ee422a171c1973d57325bcb1e4f9121bac3da` |
| `tests/test_momentum_replay.py` | `d6bf875d1719d963625fe6ce5fda070ffde04bd2269a9eb3237a82f399160935` |

在当前项目副本执行。使用离线行情，源码及测试只读检查。若当前环境只能复用依赖，用 `uv --no-cache run --locked --no-sync`。临时脚本与数据库放到分配的可写临时目录。记录 HEAD、关键文件摘要和实际 Toolkit 修订。无需重复首轮 M4-01 A–E 的全部独立脚本。

```sh
uv --no-cache run --locked --no-sync pytest -q tests/test_toolkit_adapter.py tests/test_momentum_replay.py
uv --no-cache run --locked --no-sync pytest -q
uv --no-cache run --locked --no-sync ruff check .
uv --no-cache run --locked --no-sync ruff format --check .
git diff --check
uv --no-cache run --locked --no-sync python examples/replay_round_trip.py
```

## 通过条件

1. **波动率时点与口径。** 用 `tests/fixtures/market.json` 的 A，在 2024-07-05 cutoff 计算两个单期简单收益的样本标准差。原始收盘价为 10、11、22，预期波动率约 0.636396，不年化。直接调用 Toolkit 的 `history_vol` 与 Adapter 对照。将 07-03 的 bar 改为 07-05 cutoff 之后才发布，结果须为 `None`，不得跨过缺失交易日。
2. **合成调仓。** 用 `test_composite_rotation_prefers_lower_volatility_and_resumes` 的离线价格。两个调仓日内，A 和 B 的当前单期收益相同，B 的两期波动率更低。对照 Toolkit 当日截面标准化及 `(动量 z-score − 波动率 z-score) / 2`，纯动量按 UUID 选 A，合成分数选 B，下一 tick 成交。两个特征均有效才纳入截面，截面退化为零方差或单证券时用 0。已有持仓和撮合边界沿用原规则。首批六日基线上的合成分数在 tick 3 和 5 数学上同分，最高分差在 `1e-12` 以内时须按 UUID 选 A。
3. **配置和恢复。** 动量版本 1 仍按旧配置恢复，合成策略保存版本 2 与 `volatility_window=2`，重新打开运行后继续按合成分数决策。请核对实际结果与持久化字段。M3 离线回放最终权益仍为 1,009.77 USD。

请记录逐项命令、实际数值和阻断项。需要自动解析时，最终给出单个 JSON 对象，使用启动提示中的字段。

---

<details>
<summary>首批动量调仓验收任务单（已通过）</summary>

从 `/Users/liu/QuantSim` 根目录开始。独立验收 **M4-01 的首批交付：基于历史收益的固定间隔动量调仓**。请亲自复现，并判断这条链路能否作为后续策略特征的基础。波动率、合成评分和 M4-02 绩效统计留在后续任务。

## 验收对象

交接基线为 HEAD `ab3c7048d0465eee812d67343022bb63f6cf1e48` 加当前工作区的 M4 改动。仅检出该提交会得到 M3。开始和结束时运行 `git rev-parse HEAD` 和 `git status --short --untracked-files=all`，确认以下业务文件及摘要。`AGENT.md` 与本文件是随本轮更新的交接文档。

| 文件 | 交接时 SHA-256 |
| --- | --- |
| `src/quantsim/strategy.py` | `6da53a2222da52b44be93ff1d9f42bfac1d4ae1700dd14f905fb70d158227ecc` |
| `src/quantsim/replay.py` | `79b1a439ccc946f964e2a513a5e69d76359a5e79badc084e37b75fcc894cd1a8` |
| `tests/test_momentum_replay.py`（未跟踪） | `3988ae8c5bcd1bb55b69d1de17af172942a21b0453406a4b29911682702bede4` |
| `docs/api.md` | `9970f52f1e5512e1a51d00d29c34e7b7bbb41491ac2c338f870ed3d06f170b3e` |
| `docs/architecture.md` | `bd367a88e1f4d27ff0d097b912e49c6991b69b841c7a6cacfcee45d924772ace` |
| `docs/development_guide.md` | `277ab883f89c844d615be2265e62766558da72e181c695989a919c599e7f3f12` |

开发方交接记录为 **92 项 pytest 通过，其中本轮动量回放 8 项**，Ruff 代码与格式检查通过。M3 三日示例最终权益仍为 1,009.77 USD。上述是本地回归记录，验收报告请另列实测结果。若文件缺失或内容有漂移，列出差异及影响，并标明实际验收快照。

## 工作约定与基础检查

1. 对仓库、测试、文档、锁文件和 QuantToolKit 只读。将临时脚本、数据包、数据库和报告放在系统临时目录。保留现有工作区和分支，使用离线合成数据。
2. 阅读 [项目约定](../../AGENT.md)、[M4 任务](../development_guide.md#m4-策略与绩效)、[策略接口](../api.md#strategy) 和 [回放接口](../api.md#replay)。M3 的事务与回滚沿用[此前独立验收](../development_guide.md#m3-最小回放)，本轮复查策略接入后的恢复和账本行为。
3. 记录 Python、uv、DuckDB 版本，以及实际导入的 QuantToolKit 路径、Git SHA 和工作区状态。交接环境为 Python 3.12.13、uv 0.12.22、DuckDB 2.0.0，Toolkit 从 `../quant/QuantToolKit` editable 导入，SHA 为 `b19c1969ce38f283677cc3dc5992f8df6d5dd8ad`，工作区干净。
4. 记录以下命令的退出状态、测试数和示例输出。若只能复用现有环境，可改用 `uv --no-cache run --locked --no-sync` 并说明。用临时脚本和独立数据库复现关键路径，报告时区分项目测试与额外复现。

```sh
uv run --locked pytest -q tests/test_momentum_replay.py
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
git diff --check
uv run --locked python examples/replay_round_trip.py
```

## 六日手算基线

沿用[动量测试](../../tests/test_momentum_replay.py)的合成日历和证券身份。A 与 B 分别为 `UUID(int=1)` 和 `UUID(int=2)`。回看 1 个交易日，每隔 2 个交易日调仓，目标 10 股。初始现金 200 USD，费率 10 bp，货币单位 0.01，成交量均为 100，容量比例为 1。正常 bar 在当日 cutoff 可见。

| Tick | 交易日 | A 收盘 | B 收盘 | 当日动作 | 日终现金 | 日终持仓 | 日终权益 |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: |
| 1 | 2024-07-02 | 10 | 10 | 窗口不足，跳过 | 200.00 | 空仓 | 200.00 |
| 2 | 2024-07-03 | 10 | 10 | 非调仓日 | 200.00 | 空仓 | 200.00 |
| 3 | 2024-07-05 | 12 | 10 | A 收益 20%，B 收益 0%，提交买入 A | 200.00 | 空仓 | 200.00 |
| 4 | 2024-07-08 | 12 | 10 | 买入 A 10 股，费用 0.12 | 79.88 | A 10 股 | 199.88 |
| 5 | 2024-07-09 | 11 | 16 | A 收益约 −8.33%，B 收益 60%，依次提交卖出 A、买入 B | 79.88 | A 10 股 | 189.88 |
| 6 | 2024-07-10 | 11 | 16 | 卖出 A 后买入 B，费用分别为 0.11 和 0.16 | 29.61 | B 10 股 | 189.61 |

## 验收项目

| 编号 | 独立复现及通过条件 |
| --- | --- |
| M4-01-A · 特征与调仓 | 用公开 `Replay.create`、`next_tick` 和 `step` 跑完六日。直接调用 QuantToolKit 的 `to_date_ticker_frame` 和 `historical_return`，用相同端点对照 tick 3、5 的收益与排名。核对三笔订单的提交及目标 tick 为 `(3, 4)`、`(5, 6)`、`(5, 6)`，tick 6 先卖后买。按 `simulation_id` 查询订单、成交、现金流水及日终快照，与手算表核对。末日仅处理到期订单，完成后 `next_tick()` 返回 `None`。 |
| M4-01-B · 时点与资格 | 分别令 B 在 tick 3 收益高于 A 但当日 bar 迟到、缺失回看起点或当日停牌，确认 B 不参与排名。修改 tick 4 的 B 价格，确认 tick 3 已生成的意图一致。让 A、B 同分并反转配置的证券顺序，确认仍按 UUID 选择 A。让已经持有的 A 在 tick 5 停牌，确认跳过该次调仓。 |
| M4-01-C · 版本与恢复 | 核对 `strategies` 中保存 `momentum_rotation` 版本 `1`、排序后的 UUID、回看期数、调仓间隔和股数。提交 tick 3 后退出进程，在新进程用原库和运行 ID 从 tick 4 续跑。重试已提交的 tick 5、6，对照快照和业务行，确认无重复。用 HEAD 源码在临时目录创建并提交 M3 固定策略的 tick 1，再用当前工作区从 tick 2 续跑，最终清仓权益为 1,009.77 USD。 |
| M4-01-D · 输入校验 | 使用重复证券、非正回看期或非正调仓间隔构造策略，确认返回 `INVALID_REQUEST`。用格式正确但不属于冻结批次的 UUID 创建运行，确认返回 `INVALID_REQUEST`，且未写入运行、策略定义或期初流水。 |
| M4-01-E · 两笔订单独立撮合 | 在手算基线上把初始现金改为 400 USD，在 tick 6 的收盘及 cutoff 时点停牌 A，保留其可见 bar。确认 tick 5 仍提交两单，tick 6 的 A 卖单以 `not_tradable` 结束，B 买单仍成交。日终保留 A、B 各 10 股，现金 119.72 USD，权益 389.72 USD。用该组合验证目标持仓与实际成交分离。 |

## 验收报告

- **验收对象**。记录 HEAD、文件摘要、实测环境、使用的离线数据和命令。
- **逐项结果**。对 A–E 标注 `通过`、`未通过` 或 `无法判定`，附可核对的命令输出或查询结果。分开记录实测、源码推断和未覆盖项。
- **发现**。按影响排序，给出文件与行号、触发条件、预期、实际和最小复现。区分阻断项与改进建议。
- **放行建议**。说明当前动量链路是否可以作为后续波动率与合成评分的基础。时点违规、错误的订单顺序、恢复结果不一致、重复记账和旧运行无法恢复，都需说明对放行的影响。

交付报告后由维护者决定是否放行。

</details>
