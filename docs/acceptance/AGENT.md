# QuantSim 独立复验 · M4 报告路径保护

## 验收对象

HEAD `ac6f633fea4724d4f061c78ba575fbfd4d3b5959` 加 M4-03 工作区改动及报告路径修复。QuantToolKit 为干净的 `b19c1969ce38f283677cc3dc5992f8df6d5dd8ad`。使用一次性副本及合成行情，在专用临时目录写数据库和报告。

| 本轮修改的业务文件 | SHA-256 |
| --- | --- |
| `src/quantsim/performance.py` | `92c5ea597e11b53de2faa0297459e20acb8d481286c319d1594ba11149ac8583` |
| `src/quantsim/replay.py` | `c03d4552a016c50216827a43e20f4f722ac2d5cddaa9f798a9d1f4d83f376b04` |
| `src/quantsim/toolkit_adapter.py` | `4d75c29a365998b3837bb47314ef89470ac6a455071bcee5aaa4256c943353e5` |
| `tests/test_replay.py` | `af5f300cedf162dca4fce9e002e517b919b1a27fb4f065b5ce6be9f527b6f8ac` |

其余业务文件与下方 M4-03 原验收一致。

## 复现命令

```sh
export MPLBACKEND=Agg MPLCONFIGDIR="$TMPDIR/mpl" XDG_CACHE_HOME="$TMPDIR/cache"
uv --no-cache run --locked --no-sync python -m pytest -q tests/test_replay.py -k 'report or record'
uv --no-cache run --locked --no-sync python -m ruff check .
uv --no-cache run --locked --no-sync python -m ruff format --check .
git diff --check
run_dir=$(mktemp -d "$TMPDIR/replay.XXXXXX")
uv --no-cache run --locked --no-sync python examples/replay_round_trip.py --report "$run_dir/direct" --save-run "$run_dir"
```

## 通过条件

1. 阶段快验的三条路径都返回 `INVALID_REQUEST`，数据库可重新打开，文件字节和已提交账本保持一致。分别用公开入口独立复现硬链接、缺扩展名后自动补成库名，以及自动登记报告文件名与库名重合。
2. 普通出图和自动登记均成功。省略扩展名仍按画布默认格式生成图。重复发布同摘要的报告复用已有文件，删除后可从已提交区间重建。三日样例最终权益 1,009.77 USD，费用 0.23 USD。

核对四项摘要，记录独立复现输出及任何阻断。全量回归由开发方运行。范围为本机支持硬链接的目录及单写入者。

## 复验结果

阶段快验子代理与 DeepSeek 均独立复验通过。DeepSeek 首轮跑完复现后在输出结论前超时，同一副本复跑原独立脚本后通过，四项文件摘要一致。三条冲突路径均返回 `INVALID_REQUEST`，库文件逐字节一致，正常报告可生成、复用和重建。定向回归 5 项、本地全量回归 109 项、Ruff 及差异检查均通过。正式报告 SHA-256 为 `32989df02bc3205c11fe2c5566497da3ba032aab771ae481876b715cb88a85bf`。

<!-- report-protection-history -->

<details>
<summary>M4-03 运行记录原验收</summary>

## 验收对象

HEAD `ac6f633fea4724d4f061c78ba575fbfd4d3b5959` 加本轮工作区改动。QuantToolKit 使用干净的 `b19c1969ce38f283677cc3dc5992f8df6d5dd8ad`。本机 Python 3.12.13、uv 0.12.22、DuckDB 2.0.0、pandas 3.1.1。106 项 pytest 通过，Ruff 代码和格式检查及差异检查通过，示例最终权益 1,009.77 USD，数据库和 PNG 均落盘。

在一次性副本运行离线样本，核对 HEAD、业务摘要和实际依赖导入路径。只在专用临时目录写脚本、测试数据库及报告。已跟踪与新增业务文件摘要如下。

| 文件 | SHA-256 |
| --- | --- |
| `src/quantsim/database.py` | `909b05d62234f65f671dd403d52d53bfd426bb5289571001f38f0953a35868da` |
| `src/quantsim/migrations/003_replay_results.sql` | `63bba168c762bbe822e0e44ea5d8b437618a1ca73611c30d06d90ceb9b0f2c8d` |
| `src/quantsim/performance.py` | `a8a7e16fe052ce3304c481434ab6ebc5dced701d9afa3d82cb02ca187ef0ae68` |
| `src/quantsim/replay.py` | `b3df11adc6a10369fd08623fb35cf38c208c8de633661f4a9431956ca9a20d73` |
| `tests/test_database.py` | `ab6babd11682571b25445ddab91dee0551297ad9483231b28c8d402aafb63f1b` |
| `tests/test_replay.py` | `d2178e3da7a78c1f615c1d5b0cebb2a47bcddc5ee16c464886109ee0586d25c6` |
| `examples/replay_round_trip.py` | `760976f61331e240ff975d77df189513d699c0d8a6004fa096249173220e7d0a` |
| `uv.lock` | `1c2748ca39bdb32ef6e4aa119e99c6cbf5ab03ed985ee8b4104cccdce4ec88bf` |

## 复现命令

```sh
export MPLBACKEND=Agg MPLCONFIGDIR="$TMPDIR/mpl" XDG_CACHE_HOME="$TMPDIR/cache"
uv --no-cache run --locked --no-sync python -m pytest -q
uv --no-cache run --locked --no-sync python -m ruff check .
uv --no-cache run --locked --no-sync python -m ruff format --check .
git diff --check
run_dir=$(mktemp -d "$TMPDIR/replay.XXXXXX")
uv --no-cache run --locked --no-sync python examples/replay_round_trip.py --save-run "$run_dir"
```

## 通过条件

1. **配置与重跑。** 另写一个短脚本，用公开接口运行三日 fixture。重开数据库读 `run.spec`，以新 UUID 重跑，核对相同配置、订单、成交、流水和权益的业务值。比较时排除运行派生 UUID，保留订单与成交的关联顺序。最终权益 1,009.77 USD，累计费 0.23 USD。
2. **持久化及失败恢复。** 至少保存一个未完成前缀和终态绩效，重开读取两条。核对终态收益 0.977%、基准 20.000%，报告引用和实际 SHA-256 相符。触发不存在目录的发布失败，确认账本、已有记录不变，重新发布成功。删除报告后，读记录仍可用，从已提交区间重建。重复只保存绩效不会清除已有的报告。
3. **旧库升级。** 用 `git archive HEAD` 提取旧源码到临时目录，通过旧版创建只提交前两日的版本 2 数据库。用当前源码升级并重复初始化，核对迁移版本、前两日记录及新接口，再继续提交第三日。可复用同一解释器，通过 `PYTHONPATH` 分别选择旧版和当前版，核对实际导入路径。

报告区分本地回归与额外复现，逐项记录命令、数值和阻断项。范围为合成日频、单写入者和本机 PNG，不覆盖真实行情、并发多进程写入、跨机器文件迁移或 M5。

## 验收结果

DeepSeek 独立验收通过，八项摘要与任务单一致。复跑 106 项 pytest 及 Ruff 检查，另用独立脚本验证相同配置的账本、报告登记与重建，以及旧源码创建的版本 2 数据库升级后继续回放。报告 SHA-256 为 `d356fcf143fb2815b700fc0df02f61f84791588c1b42f0fd50ca21110fde2fe7`。

</details>

<!-- acceptance-history -->

<details>
<summary>M4-02 基准与统计验收记录</summary>

## 验收对象

HEAD `d3843371bdc7724043e20a2350c7ac80dd580057` 加本轮工作区改动。开发方实测 104 项 pytest 通过，其中回放与动量两组 21 项。Ruff、格式和差异检查通过。Python 3.12.13、uv 0.12.22、DuckDB 2.0.0、pandas 3.1.1，QuantToolKit 为干净的 `b19c1969ce38f283677cc3dc5992f8df6d5dd8ad`。

使用一次性副本和离线合成行情。用 `git rev-parse HEAD`、`git status --short`、`shasum -a 256` 与实际导入路径核对快照。源码与依赖只读，脚本、数据库、报告及缓存写到分配的临时目录。

| 业务文件 | SHA-256 |
| --- | --- |
| `src/quantsim/performance.py` | `57083985f1c99ed37da5c5bd94cba2d6bf38aee5417d8da41f6c85302dd3bd44` |
| `src/quantsim/replay.py` | `0d22b3069fffa0214d246ed4e08939c69c6eb4ce0b37cdefcce9e87a23d2352f` |
| `src/quantsim/toolkit_adapter.py` | `d97a5da3095fa335227e867e8811ea98bdbe25314c3e942fa33cf656eefddd63` |
| `tests/test_replay.py` | `2215db55a744940849df579a386f2e3bc0773621a443186591ebbd6420400b77` |
| `tests/test_momentum_replay.py` | `b63dec4bbf6ec045ac0a472ebadee7e9d328ca7bbb0cb364964b2ad440e77ff5` |
| `examples/replay_round_trip.py` | `45ed82e58249877e3cfe00225b960a6d9a4b1b37d1e8379b356f0a15a9a2119a` |
| `uv.lock` | `1c2748ca39bdb32ef6e4aa119e99c6cbf5ab03ed985ee8b4104cccdce4ec88bf` |

## 复现命令

```sh
export MPLBACKEND=Agg MPLCONFIGDIR="$TMPDIR/mpl"
uv --no-cache run --locked --no-sync python -m pytest -q
uv --no-cache run --locked --no-sync ruff check .
uv --no-cache run --locked --no-sync ruff format --check .
git diff --check
uv --no-cache run --locked --no-sync python examples/replay_round_trip.py --report "$TMPDIR/replay.png"
```

## 通过条件

1. **账本到指标。** 用公开 Replay 入口运行三日样本，并独立手算策略收益 `[0, -0.11/1000, 9.88/999.89]`。确认三日权益 1000.00、999.89、1009.77 USD，总费 0.23 USD。累计收益 0.977%，最大回撤 −0.011%，胜率 1/3，夏普为日收益均值除以样本标准差。打开旧式三日运行或在中途重新打开同一库，统计应与已提交前缀一致，重复读取不会推进。空运行无指标，一日策略夏普未定义。
2. **固定份额基准。** 用三日样本确认首日无收益，之后为 `11/10 - 1`、`12/11 - 1`，累计 20%。再用两证券动量样本 A=`[10,10,12,12,11,11]`、B=`[10,10,10,10,16,16]` 验证基准累计 35%。将 B 的第三日 bar 改为第六日才可见，确认第三、四日基准收益缺失，缺价日期保留，基准区间指标未定义，策略统计仍可读。首日缺一只证券时整段基准未建仓。
3. **报告及异常。** 核对净值与回撤图在离线样本实际生成，净值图策略终点为 1.00977、基准终点为 1.2。报告保存失败后，已提交结果仍可重新生成。删除中间检查点后，绩效必须报 `INCONSISTENT_STATE`，不得跨断档计算。

请至少另做一次独立的公开接口复现，与测试及三日命令输出区分。记录实际命令和数值。报告不覆盖真实数据、公司行动、长区间性能或 M4-03 的报告索引持久化。

## 验收结果

DeepSeek 独立复验通过。首次模型会话完成 104 项全量测试、21 项局部测试、Ruff、出图和独立脚本，生成结论前超时。同一副本复验核对了七项摘要，重新执行独立脚本及示例，输出与首次复现一致，无阻断项。报告摘要见 [开发指南](../development_guide.md#m4-策略与绩效)。

</details>

<details>
<summary>M4-01 波动率与合成调仓验收记录</summary>

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

</details>

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
