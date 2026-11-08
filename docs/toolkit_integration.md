# QuantToolKit 审计与集成

审计对象为本机 `/Users/liu/quant/QuantToolKit` 的 `main`，提交 `94532f91e0708ca20713b7660477da130e1aa6d6`，项目版本 `0.1.0`。源码和现有 DuckDB 只读审计。QuantSim 开发环境通过可编辑安装加载本机 QuantToolKit 工作目录。复现审计结果时使用上述提交。

当前 [CI 固定版本](../.github/workflows/ci.yml) `b19c196` 的目录实测有 78 项契约。策略通道启用了 `panel.returns.historical_return`、`panel.volatility.history_vol` 和 `factors.combine.combine_scores`。合成分数使用 `factors.zscore.zscore_by_date` 做当日截面标准化。下文逐项清单对应早期 70 项审计基线。

## 目录、依赖与回测能力

| 路径 | 已验证的职责 |
| --- | --- |
| `src/quanttoolkit/sources/`、`db/`、`data_pipeline.py` | Yahoo OHLCV、Wikipedia 当前 S&P 500、French ZIP，DuckDB 建表、写入和 `factor_panel` 视图。pipeline 按表提交，联网刷新可能留下部分结果 |
| `panel/`、`factors/` | pandas 长表转换、缺行填补、历史及未来收益与波动、动量、反转、对数波动、RSI、CMA 暴露及截面加工 |
| `portfolio/`、`metrics.py`、`figure/` | 调仓日历、分位目标权重、等权买入持有基准、IC、组合指标及 Matplotlib 报告 |
| `model/` | 标签结束日、样本对齐、按日历和实际结束日切分、LightGBM 回归及 LambdaRank、手写树和 GBDT、参数和训练记录 |
| `registry.py`、`_catalog.py`、`tracelog.py` | 契约查询、部分输入检查及对象来源记录、日志配置和调用跟踪 |
| `tests/`、`.github/workflows/ci.yml` | 合成数据和替身覆盖计算、数据接入、注册表、报告、模型及离线示例。CI 配置为 Ubuntu/Python 3.12，执行锁定安装、Ruff、带 branch coverage 的 pytest 和构建 |
| `docs/`、`scripts/`、`research/`、`data/`、`logs/`、`dist/` | 调用说明、接入脚本和离线示例、独立研究、运行数据、日志和构建产物 |

`pyproject.toml` 要求 Python ≥3.12，用 `uv_build>=0.12.22,<0.13.0` 构建。运行依赖下限为 `duckdb 1.5.6`、`lightgbm 4.7.0`、`loguru 0.7.3`、`lxml 6.0`、`matplotlib 3.11.2`、`numpy 2.5.4`、`pandas 3.0.6`、`scikit-learn 1.9.1`、`statsmodels 0.15.0`、`torch 2.14.1`、`yfinance 1.7.0`。开发组为 `ipykernel>=7.4.0`、`pytest>=9.1.1`、`pytest-cov>=7.1.0` 和 `ruff>=0.17.0`。审计的包源码里未见 Torch、statsmodels 或 scikit-learn 的直接导入。安装上游包仍会解析其声明的运行依赖，LightGBM 和 Torch 等原生包的 macOS 安装在 M1 实测。

`vectorized` 和 `event_driven` 是报告和绩效条目的流程分类。包内可复用回测原语为调仓日期、目标权重、基准收益、绩效和图表。持有期标签属于研究数据。未找到可调用的完整向量化回测入口或事件驱动撮合入口。Simulator 提供事件时序、订单、资金约束和持仓账本，把成交后的组合单期收益交给工具箱计算指标。

`factor_panel` 是 Toolkit 自己的研究 SQL 视图。它按证券计算隔夜、日内、量变、动量、波动及相对成交量，从全库第 64 个不同交易日起与 FF5 内连接。Simulator 可离线使用冻结且有时点证明的导出产物。视图会随底层表变化，且其口径包含内部缺行的补齐价格。

## 测试状态

- 本机 macOS arm64 / Python 3.12.13，使用 Toolkit 已有 `.venv`。实测 DuckDB 1.5.6、pandas 3.0.6、LightGBM 4.7.0、Matplotlib 3.11.2、pytest 9.1.1。
- 全量 `python -m pytest -q -p no:cacheprovider` 为 **285 passed**，24.09 秒。Ruff lint 通过，format 检查 112 个文件已格式化，`git diff --check` 通过。
- 契约测试核对登记参数与真实签名及上游引用，验证复制后严格入口拒绝、价格列校验、标签期限不符、已知价差误入净值以及三种报告产物。Yahoo 和下载管线主要通过测试替身验证。真实网络、数据授权、全市场时点可用性以及 Simulator 撮合的正确性要独立验证。
- 独立使用 `--cov=quanttoolkit --cov-branch --cov-report=term-missing` 再跑全量，285 项通过，23.69 秒。语句和分支合并覆盖率 96%（1,498 条语句缺 37 条，390 个分支有 38 个部分覆盖）。`registry.py` 85%，单图 90%，报告 96%，树 92%，GBDT 91%。未覆盖部分包括来源检查的异常分支及部分绘图错误。

## 数据与来源契约

- 行情标准形状为 `pd.DataFrame`，索引顺序和名称为 `[date, ticker]`。严格面板检查要求唯一键，日期层为 `DatetimeIndex`，键无缺失，证券代码为非空字符串。严格价格检查只看调用选中的 `price_col`，须为唯一实数列，非缺失值有限且大于 0。静态名单和 DuckDB 查询结果是普通列长表。
- 标签、单因子、分数、权重多为同索引的 `pd.Series`。研究标签期限按各证券记录计数。组合收益、IC 和价差各自为日期索引的 Series，但经济含义不同。组合收益为连续非重叠单期小数收益，远期价差可重叠，逐笔滑点用 bps 并允许时间重复。
- `list_contracts(stage=..., workflow=...)` 返回筛选后的契约字典副本，`get_contract(name)` 返回单条副本，未知名抛 `KeyError`。目录本身不导入业务模块。审计基线有 70 项，38 项接入 `@_contract`，26 项追踪返回对象。装饰器绑定真实签名并应用默认值后检查输入，成功返回后按条目记录对象与期限元数据。每项输出定义是描述。
- `required` 要求当前对象来自明确允许的入口。`if_known` 允许未知对象，但会拒绝已知且不在清单中的来源。`described` 仅描述上游，仍可能配置格式检查或由函数体校验。8 个严格参数是 5 个收益/波动接口的 `df`、`split_samples.label_end`、净值和回撤图的 `portfolio_returns`。
- Toolkit 来源由进程内弱引用和对象身份关联。复制、切片、拼接和库重读会产生新对象。原地修改可能保留身份，因此来源记录也无法证明数据内容不变。Simulator 保存批次指纹、转换记录和经济含义，在同一调用链保留已登记对象。对经过合法转换的新数据，在对应入口重新接入并传递入口的返回值。

### 对本地数据的核对

当前 `data/quanttoolkit.db` 含 `prices`、`universe`、`ff_factors`、`factor_panel`，另有 `instant_facts`、`period_facts`。只读查询的 `prices` 共 1,315,667 行、503 个 ticker，日期从 2016-01-04 至 2026-10-09，2,743 行 `volume=0`，所有行 `close=adj_close`。一键 pipeline 拉取**当前**名单并使用 `auto_adjust=True`，行情 DDL 未保存复权标记、历史成员有效期、可交易标记或可用时刻。仅凭库内容无法还原真实成交价格和当时名单。初期使用固定测试行情跑成交和账本。本地历史数据可在标注局限后用于离线研究或演示，历史成交输入需独立提供价格口径、公司行动范围和来源证据。

### 需要在 Simulator 明确处理的差异

| 观察 | 集成决定 |
| --- | --- |
| `to_date_ticker_frame` 会改输入的 `date` 列，转换本身允许重复键 | 给它独占的新长表，预查重复并由后续严格入口检查面板 |
| 实测 `historical_return(n_periods=-1)` 和 `base_momfactor(skip=-1)` 都能读取未来行 | 策略通道对窗口、期限、`skip`、`gap`、`freq` 检查整数域，并按 `as_of` 构造可见输入。预热行不足由策略返回空决策 |
| `prepare_portfolio_returns` 检查形状及有限性，实测接收 -1.1。常量负收益的 `sharpe` 实测返回正无穷 | 组合收益从有效 NAV 快照生成，校验期序、期初权益、收益下界。非有限绩效值以未定义状态呈现，保留工具箱原始诊断值 |
| `equal_weight_hold` 首期为 NaN，空返回的索引也可能并非日期型 | 非空基准只去掉预期的首期 NaN，再验证剩余期序和数值，最后重新登记。中途缺值保留为数据问题 |
| `fill_price_gaps` 按输入已有日历补证券首末记录之间的行，前填除 volume 外的列，新行 volume 为 0 | 仅研究视图可补齐，标记合成行，且研究实时切片先按可用时间截断。撮合只用实际有量的原始 bar |
| `read_french_factors` 目录描述 CSV/ZIP，函数实际调用 `ZipFile`，读取首个成员 | 导入入口限定结构匹配的日频 ZIP，保留文件指纹和发布日期信息 |
| `operation_sql` 自行开关事务，用 `df` 供 DuckDB replacement scan。返回值读取在提交前，但转 `int` 在提交后 | 只在独立工具箱接入任务中使用。Simulator 的一步落库由自身事务统一管理 |
| `assemble_samples` 和 `split_samples` 仅在来源元数据仍存在时核对期限 | 持久化同步保存行情版本、标签参数及逐行结束日。重载时先核对这些信息，再走公开接入方法 |

## Adapter 的调用路径

1. 启动时通过 `registry.list_contracts()` 查询目录。每个已启用的 Simulator 能力绑定固定的公开 callable、预期调用形状和允许通道。清单按需加载模块，M1 验证必需条目和签名。目录新增条目先经过审查。策略接口见 [api.md](api.md)。
2. 从 `MarketDataRepository` 接收带批次、时点和价格口径的行情。长表使用 `to_date_ticker_frame(df=owned_frame)`，已有面板用 `prepare_price_panel(prices)`。保留返回对象用于下游。真实的标签结束日和组合收益分别走 `prepare_label_end_dates` 与 `prepare_portfolio_returns`。因子远期价差单独走 `prepare_forward_spread`。
3. 调用原始公开入口及其包装器。仅在交付领域结果或导出时转换。需要在 Toolkit 内继续组合时保留原对象。跨进程传输按已知类型和公开入口重新登记，记录批次、`as_of`、参数、用途及结果口径。对已经带来源且与目标不兼容的对象直接返回错误。对来源已失的对象按持久化的类型和谱系判断是否可接入。
4. 把参数绑定、已知输入校验、依赖缺失、I/O 和意外计算错误映射为带能力 ID 的 Simulator 错误，保留异常链。Toolkit 的业务 `ValueError` 同时用于来源和数值校验，统一归到工具箱输入拒绝，避免依赖异常文本做分类。数据来源或时间违规若由 Adapter 自己查出，则提供独立代码。完整调用不会修改 Toolkit 源码或私有登记表。

## 逐接口能力清单

以下名称均以 `quanttoolkit.` 为隐含前缀。签名来自可调用对象。未加注解的参数或返回用实现核对。每行的上游是目录里登记的数据依赖，另在入口条件补充实现依赖。来源列的 **必需** 对应 `required`，**已知时** 对应 `if_known`。只有列出的参数有运行时来源门槛。**无** 的参数仍须满足格式、业务或时点约束。`T` 表示 Toolkit 追踪返回对象。`P`、`F` 等为下列精确上游集合的缩写。

- `P` = `panel.transfer.to_date_ticker_frame`、`panel.transfer.prepare_price_panel`、`sources.yahoo.get_prices`、`panel.prices.fill_price_gaps`
- `F` = `factors.mom_base.base_momfactor`、`factors.reversal_base.base_reversalfactor`、`factors.logvol_base.base_logvolfactor`、`factors.rsi_base.base_rsifactor`、`factors.exposure.cma_exposure`、`factors.neutral.neutralize_by_industry`、`factors.zscore.zscore_by_date`
- `S` = `factors.mom_base.base_momfactor`、`factors.reversal_base.base_reversalfactor`、`factors.logvol_base.base_logvolfactor`、`factors.rsi_base.base_rsifactor`、`factors.exposure.cma_exposure`、`factors.combine.combine_scores`、`model.lambdarank.predict_scores`、`panel.returns.historical_return`、`panel.returns.log_returns`、`panel.volatility.history_vol`
- `R` = `metrics.prepare_portfolio_returns`、`portfolio.benchmark.equal_weight_hold`
- `I` = `metrics.ic`、`metrics.rank_ic`
- `W` = `portfolio.sizing.simple_quantile_long_short`、`portfolio.sizing.vol_quantile_long_short`
- `B` = `model.lightgbm.fit_lightgbm`、`model.lambdarank.fit_lambdarank`
- `X` = `model.split.split_samples`、`model.dataset.assemble_samples`
- `Y` = `panel.returns.future_returns`、`panel.volatility.forward_volatility`、`model.split.split_samples`

### 数据接入与行情准备（10）

| 接口及真实签名 | 入口条件与数据口径 | 已登记上游（参数 → 接口） | 来源门槛 | Simulator 用途 |
| --- | --- | --- | --- | --- |
| `sources.yahoo.get_prices`<br>`(tickers: 'str \| Sequence[str]', start: 'DateLike', end: 'DateLike \| None' = None, *, interval: 'str' = '1d', auto_adjust: 'bool' = False, progress: 'bool' = True) -> 'pd.DataFrame'` | Yahoo 网络与 yfinance，缺 close 会丢行，失败 ticker 重试后可能省略。返回排序 OHLCV，日期去时区。auto_adjust=True 时缺 adj_close 用 close | — | 无 · T | 离线导入 |
| `sources.french.read_french_factors`<br>`(path: 'str \| Path') -> 'pd.DataFrame'` | 可读 ZIP，首成员含 ,Mkt-RF 表头和 YYYYMMDD 日频行。返回 date 索引与小数因子列 | — | 无 | 离线研究 |
| `sources.wikipedia.fetch_sp500_universe`<br>`() -> pandas.DataFrame` | 网络与 lxml。返回当前名单普通列 ticker/name/sub_industry，点号转连字符 | — | 无 | 离线导入 |
| `db.connection.get_duckdb`<br>`(*, path: 'Path', read_only: 'bool' = True) -> 'duckdb.DuckDBPyConnection'` | 只读文件须存在，可写时创建父目录。调用方关闭连接 | `path` → `data_pipeline.main` | 无 | 离线读取工具箱库 |
| `db.operation.read_sql`<br>`(sql: 'Path') -> 'str'` | UTF-8 文件存在可读 | — | 无 | 离线工具 |
| `db.operation.operation_sql`<br>`(sql: 'str', ddl: 'str', connection: 'duckdb.DuckDBPyConnection', df: 'pd.DataFrame \| None' = None) -> 'int'` | 可写连接，DDL 与 SQL 字符串。写入 SQL 可用 df replacement scan，末语句首列须可转整数 | `sql/ddl` → `db.operation.read_sql`<br>`connection` → `db.connection.get_duckdb` | 无 | 离线工具 |
| `data_pipeline.main`<br>`(*, logger_path: 'str \| Path \| None' = None, data_path: 'str \| Path \| None' = None) -> 'Path'` | 目录可写、联网及来源完整。分表写入，创建 factor_panel，返回数据库绝对路径 | — | 无 | 离线独立任务 |
| `panel.transfer.to_date_ticker_frame`<br>`(*, df: 'pd.DataFrame') -> 'pd.DataFrame'` | 普通列 date/ticker，date 可解析。会改输入 date，输出排序面板，调用方确保键唯一 | — | 无 · T | 接入 |
| `panel.transfer.prepare_price_panel`<br>`(prices: 'pd.DataFrame') -> 'pd.DataFrame'` | 已有合法面板。浅复制并登记，价格列由下游核验 | `prices` → `P` | `prices` 已知时 · T | 接入 |
| `panel.prices.fill_price_gaps`<br>`(prices: 'pd.DataFrame') -> 'pd.DataFrame'` | 合法面板。取已出现日期补内部缺行，除 volume 外前填，新增 volume=0 | `prices` → `P` | `prices` 已知时 · T | 研究限定 |

### 收益、波动及因子（13）

| 接口及真实签名 | 入口条件与数据口径 | 已登记上游（参数 → 接口） | 来源门槛 | Simulator 用途 |
| --- | --- | --- | --- | --- |
| `panel.returns.future_returns`<br>`(*, df: 'pd.DataFrame', n_periods: 'int' = 5, gap: 'int' = 1, price_col: 'str' = 'adj_close') -> 'pd.Series'` | 选中价格列合法。n_periods≥1、gap≥0，逐证券 t+gap 至 t+gap+n_periods，缺端点为 NaN | `df` → `P` | `df` 必需 · T | 离线标签 |
| `panel.returns.historical_return`<br>`(*, df: 'pd.DataFrame', n_periods: 'int' = 5, price_col: 'str' = 'adj_close') -> 'pd.Series'` | 选中价格列合法，n_periods≥1，逐证券排序，回看 t-n_periods，缺端点为 NaN | `df` → `P` | `df` 必需 · T | 策略特征 |
| `panel.returns.log_returns`<br>`(*, df: 'pd.DataFrame', price_col: 'str' = 'adj_close') -> 'pd.Series'` | 选中价格列合法，按证券相邻行取对数差，首行或缺价为 NaN | `df` → `P` | `df` 必需 · T | 策略特征 |
| `panel.volatility.history_vol`<br>`(*, df: 'pd.DataFrame', window: 'int' = 21, price_col: 'str' = 'close') -> 'pd.Series'` | 选中价格列合法，window≥2，样本标准差 ddof=1，按证券至少 window+1 条价格，不年化 | `df` → `P` | `df` 必需 · T | 策略特征 |
| `panel.volatility.forward_volatility`<br>`(*, df: 'pd.DataFrame', window: 'int' = 21, gap: 'int' = 1, price_col: 'str' = 'close') -> 'pd.Series'` | 选中价格列合法，window≥2、gap≥0，未来 window 个简单收益的样本标准差，不年化 | `df` → `P` | `df` 必需 · T | 离线标签 |
| `factors.mom_base.base_momfactor`<br>`(prices: 'pd.DataFrame', window: 'int', *, skip: 'int' = 0, price_col: 'str' = 'adj_close') -> 'pd.Series'` | 选中价格列合法，window≥1、skip≥0，按证券滚动对数收益之和再后移 | `prices` → `P` | `prices` 已知时 · T | 策略特征 |
| `factors.reversal_base.base_reversalfactor`<br>`(prices: 'pd.DataFrame', window: 'int', *, skip: 'int' = 0, price_col: 'str' = 'adj_close') -> 'pd.Series'` | 与动量同输入和范围，实际调用 base_momfactor 取负 | `prices` → `P` | `prices` 已知时 · T | 策略特征 |
| `factors.logvol_base.base_logvolfactor`<br>`(prices: 'pd.DataFrame', window: 'int', *, price_col: 'str' = 'adj_close') -> 'pd.Series'` | 选中价格列合法，window≥2，按证券滚动对数收益 ddof=1，不年化 | `prices` → `P` | `prices` 已知时 · T | 策略特征 |
| `factors.rsi_base.base_rsifactor`<br>`(prices: 'pd.DataFrame', window: 'int' = 14, *, price_col: 'str' = 'adj_close') -> 'pd.Series'` | 选中价格列合法，window≥1。Wilder 平滑，缺价重启预热，涨跌皆零时 NaN | `prices` → `P` | `prices` 已知时 · T | 策略特征 |
| `factors.exposure.cma_exposure`<br>`(prices: pandas.DataFrame, ff: pandas.DataFrame, window: int) -> pandas.Series` | 非空且已排序面板含 adj_close/volume。ff 为唯一同口径 date 索引，含 rf 和五因子小数列。正窗口且足以拟合，非正量或缺值影响窗口完整性 | `prices` → `P`<br>`ff` → `sources.french.read_french_factors` | 无 · T | 离线研究，时点数据齐备后可接策略 |
| `factors.neutral.neutralize_by_industry`<br>`(factors: 'pd.DataFrame', industry: 'pd.Series', *, date_level: 'str' = 'date', ticker_level: 'str' = 'ticker') -> 'pd.DataFrame'` | 数值因子面板和唯一 ticker 索引行业映射。按日期行业减均值，缺行业为 NaN | `factors` → `F`<br>`industry` → `sources.wikipedia.fetch_sp500_universe` | 无 | 策略特征，要求时点行业映射 |
| `factors.zscore.zscore_by_date`<br>`(factors: 'pd.DataFrame', *, date_level: 'str' = 'date') -> 'pd.DataFrame'` | 数值表有 date_level，按日用样本标准差，零方差或样本不足为 NaN | `factors` → `F` | 无 | 策略特征 |
| `factors.combine.combine_scores`<br>`(z: 'pd.DataFrame', weights: 'dict[str, float] \| None' = None) -> 'pd.Series'` | 数值表与权重字典，键须对应列。未指定列权重为 0，weights=None 各列等权。用有效列绝对权重之和归一 | `z` → `F` | 无 · T | 策略信号 |

### 策略与基准（5）

| 接口及真实签名 | 入口条件与数据口径 | 已登记上游（参数 → 接口） | 来源门槛 | Simulator 用途 |
| --- | --- | --- | --- | --- |
| `portfolio.rebalance.rebalance_dates`<br>`(dates: 'pd.DatetimeIndex', freq: 'int') -> 'pd.DatetimeIndex'` | 共同时点交易日历，freq 正整数。排序去重后取每第 freq 个日历日 | `dates` → `P` | 无 | 策略调度 |
| `portfolio.rebalance.rebalance_dates_belong`<br>`(dates: 'pd.DatetimeIndex', freq: 'int') -> 'pd.Series'` | 共同日历与正整数 freq。返回每个日期对应的最近历史调仓日 | `dates` → `P` | 无 | 离线研究或调度 |
| `portfolio.sizing.simple_quantile_long_short`<br>`(*, score: 'pd.Series', n_quantiles: 'int', date_level: 'str' = 'date') -> 'pd.Series'` | score 为按日数值面板，n_quantiles≥2。去 NaN，样本不足或分桶退化跳过当日。多空两腿各绝对权重 1 | `score` → `S` | `score` 已知时 · T | 模拟扩展，须有空头约束 |
| `portfolio.sizing.vol_quantile_long_short`<br>`(*, score: 'pd.Series', vol: 'pd.Series', n_vol_groups: 'int', n_quantiles: 'int', date_level: 'str' = 'date') -> 'pd.Series'` | score/vol 面板按键内连接，n_vol_groups≥1、n_quantiles≥2。风险分层后选极端分组，输出只含两腿，各腿绝对权重 1 | `score` → `S`<br>`vol` → `panel.volatility.history_vol`、`factors.logvol_base.base_logvolfactor` | `score` 已知时 · T | 模拟扩展，须有空头约束 |
| `portfolio.benchmark.equal_weight_hold`<br>`(*, prices: 'pd.DataFrame', price_col: 'str' = 'adj_close') -> 'pd.Series'` | 选中价格列合法，首日非缺价证券入选，固定份额。首期 NaN，中途缺价影响收益 | `prices` → `P` | `prices` 已知时 · T | 回放基准 |

### 样本、训练与预测（15）

| 接口及真实签名 | 入口条件与数据口径 | 已登记上游（参数 → 接口） | 来源门槛 | Simulator 用途 |
| --- | --- | --- | --- | --- |
| `model.dataset.label_end_dates`<br>`(index: 'pd.MultiIndex', *, n_periods: 'int' = 5, gap: 'int' = 1) -> 'pd.Series'` | 完整合法面板索引，n_periods≥1、gap≥0。逐证券找第 gap+n_periods 条后续记录，不足为 NaT | `index` → `P` | 无 · T | 离线标签 |
| `model.dataset.prepare_label_end_dates`<br>`(values: 'pd.Series') -> 'pd.Series'` | 外部实际结束日期为面板 Series，允许 NaT，返回浅复制，继承已有期限元数据 | `values` → `model.dataset.label_end_dates`、`model.dataset.prepare_label_end_dates` | `values` 已知时 · T | 离线标签接入 |
| `model.dataset.assemble_samples`<br>`(*, features: 'pd.DataFrame', label: 'pd.Series') -> 'pd.DataFrame'` | 特征与标签索引名相同且唯一，标签有唯一列名。内连接，删含缺失行，标签置末，继承标签期限 | `features` → `X`<br>`label` → `Y` | 无 · T | 离线模型 |
| `model.split.split_samples`<br>`(*, samples: 'pd.DataFrame', dates: 'pd.DatetimeIndex', label_end: 'pd.Series', train_end: 'str \| pd.Timestamp', valid_end: 'str \| pd.Timestamp', embargo: 'int') -> 'dict[str, pd.DataFrame]'` | 共同日历、同口径截止日、embargo≥0、逐行结束日。双边均有期限元数据时须匹配，删 NaT，train/valid 标签结束不得跨界 | `samples` → `model.dataset.assemble_samples`、`panel.transfer.to_date_ticker_frame`<br>`dates` → `P`<br>`label_end` → `model.dataset.label_end_dates`、`model.dataset.prepare_label_end_dates` | `samples` 已知时、`label_end` 必需 | 离线模型 |
| `model.split.split_dates`<br>`(dates: 'pd.DatetimeIndex', *, train_end: 'str \| pd.Timestamp', valid_end: 'str \| pd.Timestamp', embargo: 'int') -> 'pd.Series'` | 共同日历、train_end≤valid_end、embargo≥0、日期时区一致。返回 train/valid/test，隔离期 NA | `dates` → `P` | 无 | 离线模型 |
| `model.relevance.relevance_by_date`<br>`(label: 'pd.Series', *, n_grades: 'int' = 31) -> 'pd.Series'` | 面板标签含 date 层，正整数档位。按截面 rank first 分档，输出 nullable Int64。给 LambdaRank 用最多 31 档 | `label` → `Y` | 无 | 离线模型 |
| `model.lambdarank.group_sizes`<br>`(index: 'pd.MultiIndex') -> 'np.ndarray'` | 按 date 连续分组的面板索引，统计每段长度，不负责重排 | `index` → `model.split.split_samples` | 无 | 离线模型 |
| `model.lambdarank.fit_lambdarank`<br>`(*, features: 'pd.DataFrame', relevance: 'pd.Series', params: 'LambdaRankParams \| None' = None) -> 'lgb.Booster'` | 特征非空且数值，相关度对齐且为 0..30 整数。先排序再调用 group_sizes，LightGBM Booster | `features` → `X`<br>`relevance` → `model.relevance.relevance_by_date` | 无 | 离线模型 |
| `model.lightgbm.fit_lightgbm`<br>`(*, features: 'pd.DataFrame', label: 'pd.Series', valid_features: 'pd.DataFrame \| None' = None, valid_label: 'pd.Series \| None' = None, params: 'LightGBMParams \| None' = None) -> 'lgb.Booster'` | 非空数值特征、有限对齐标签。验证特征和标签成对，列名和顺序一致，早停配置在 params | `features/valid_features` → `X`<br>`label/valid_label` → `Y` | 无 | 离线模型 |
| `model.tree.RegressionTree.fit`<br>`(self, X, y)` → 已拟合的 RegressionTree 自身 | X 为有限数值二维 array-like，y 为有限数值一维 array-like，非空等长。原地训练并返回 self | — | 无 | 教学实验 |
| `model.tree.RegressionTree.predict`<br>`(self, X)` → 逐行预测值 ndarray | 拟合过的 self、二维数值 array-like X，与训练特征一致。返回浮点 ndarray | `self` → `model.tree.RegressionTree.fit` | 无 | 教学实验 |
| `model.tree.RegressionTree.apply`<br>`(self, X)` → 逐行叶子编号 ndarray | 拟合过的 self、二维数值 array-like X，与训练特征一致。返回整型叶子编号 ndarray | `self` → `model.tree.RegressionTree.fit` | 无 | 教学实验 |
| `model.gbdt.SimpleGBDT.fit`<br>`(self, X, y, X_val=None, y_val=None, early_stopping_rounds=None)` → 已拟合的 SimpleGBDT 自身 | 有限 X/y 同回归树，验证对成对且列数一致。early_stopping_rounds 为正整数并要求验证集。用 RegressionTree，返回 self | — | 无 | 教学实验 |
| `model.gbdt.SimpleGBDT.predict`<br>`(self, X)` → 逐行预测值 ndarray | 拟合过的 self、二维数值 array-like X，与训练特征一致。返回浮点 ndarray | `self` → `model.gbdt.SimpleGBDT.fit` | 无 | 教学实验 |
| `model.lambdarank.predict_scores`<br>`(*, booster: 'lgb.Booster', features: 'pd.DataFrame') -> 'pd.Series'` | 已训练或受信任文件恢复的 Booster。特征列名及顺序匹配，保留原行索引，返回 pred | `booster` → `B`<br>`features` → `X` | 无 · T | 预训练推理，核对训练截止与特征谱系 |

### 统计与报告（16）

| 接口及真实签名 | 入口条件与数据口径 | 已登记上游（参数 → 接口） | 来源门槛 | Simulator 用途 |
| --- | --- | --- | --- | --- |
| `metrics.ic`<br>`(*, factor: 'pd.Series', forward_return: 'pd.Series') -> 'pd.Series'` | 因子与未来收益均为数值面板，内连接后逐日 Pearson，有效配对不足或常量为 NaN | `factor` → `S`<br>`forward_return` → `panel.returns.future_returns` | `factor` 已知时、`forward_return` 已知时 · T | 离线归因 |
| `metrics.rank_ic`<br>`(*, factor: 'pd.Series', forward_return: 'pd.Series') -> 'pd.Series'` | 因子与未来收益均为数值面板，内连接后逐日 Spearman，有效配对不足或常量为 NaN | `factor` → `S`<br>`forward_return` → `panel.returns.future_returns` | `factor` 已知时、`forward_return` 已知时 · T | 离线归因 |
| `metrics.ic_summary`<br>`(*, ic: 'pd.Series') -> 'pd.Series'` | 逐日相关 Series，跳过 NaN。返回 ic_mean/ic_std/ic_ir/ic_pos，IR 未年化 | `ic` → `I` | `ic` 已知时 | 离线归因 |
| `metrics.prepare_portfolio_returns`<br>`(returns: 'pd.Series') -> 'pd.Series'` | 唯一无缺失日期，非空有限数值。连续性、非重叠及收益下界由调用方证明，浅复制 | `returns` → `R` | `returns` 已知时 · T | 绩效接入 |
| `metrics.prepare_forward_spread`<br>`(spread: 'pd.Series') -> 'pd.Series'` | 唯一无缺失日期，实数，可含 NaN 和重叠窗口，拒绝无穷值，浅复制 | `spread` → `metrics.prepare_forward_spread` | `spread` 已知时 · T | 离线归因接入 |
| `metrics.total_return`<br>`(*, returns: 'pd.Series') -> 'float'` | 组合简单收益，排序后复利。空或含 NaN 返回 NaN，调用方先检查有限性 | `returns` → `R` | `returns` 已知时 | 回放绩效 |
| `metrics.sharpe`<br>`(*, returns: 'pd.Series') -> 'float'` | 组合单期简单收益，均值除 ddof=1 标准差，无年化或额外无风险扣减，退化值见上文 | `returns` → `R` | `returns` 已知时 | 回放绩效 |
| `metrics.max_drawdown`<br>`(*, returns: 'pd.Series') -> 'float'` | 组合单期简单收益，初始峰值 1。输出非正小数，空或含 NaN 返回 NaN | `returns` → `R` | `returns` 已知时 | 回放绩效 |
| `metrics.win_rate`<br>`(*, returns: 'pd.Series') -> 'float'` | 一维组合收益或逐信号日价差，严格正值占比，空或含 NaN 返回 NaN | `returns` → `metrics.prepare_portfolio_returns`、`portfolio.benchmark.equal_weight_hold`、`metrics.prepare_forward_spread` | `returns` 已知时 | 绩效或离线归因 |
| `figure.plots.plot_nav`<br>`(portfolio_returns: 'pd.Series', *, ax: 'Axes \| None' = None) -> 'Axes'` | 已登记非空有限组合收益、唯一无缺失日期。可给 Axes，返回同轴或新轴 | `portfolio_returns` → `R` | `portfolio_returns` 必需 | 报告 |
| `figure.plots.plot_drawdown`<br>`(portfolio_returns: 'pd.Series', *, ax: 'Axes \| None' = None) -> 'Axes'` | 与净值图同输入，以初始净值 1 作峰值下界。可给 Axes | `portfolio_returns` → `R` | `portfolio_returns` 必需 | 报告 |
| `figure.plots.plot_target_exposure`<br>`(target_weights: 'pd.Series', *, ax: 'Axes \| None' = None) -> 'Axes'` | 非空唯一面板权重，有限数值。逐日绘 gross/net，空头为负。可给 Axes | `target_weights` → `W` | `target_weights` 已知时 | 向量化研究报告 |
| `figure.plots.plot_execution_slippage`<br>`(slippage_bps: 'pd.Series', *, ax: 'Axes \| None' = None) -> 'Axes'` | 成交时间 DatetimeIndex，可重复，有限 bps，可空，正值表示不利。可给 Axes | — | `slippage_bps` 已知时 | 事件回放报告 |
| `figure.plots.plot_ic`<br>`(ic: 'pd.Series', *, ax: 'Axes \| None' = None) -> 'Axes'` | 非空唯一日期相关系数，可含 NaN。可给 Axes | `ic` → `I` | `ic` 已知时 | 研究报告 |
| `figure.plots.plot_forward_spread`<br>`(forward_spread: 'pd.Series', *, ax: 'Axes \| None' = None) -> 'Axes'` | 非空唯一日期，远期价差小数，可含 NaN。可给 Axes | `forward_spread` → `metrics.prepare_forward_spread` | `forward_spread` 已知时 | 研究报告 |
| `figure.report.plot_report`<br>`(kind: "Literal['vectorized', 'event_driven', 'factor_research']", *, output: 'str \| Path \| None' = None, **artifacts: 'pd.Series') -> 'Figure'` | vectorized/event_driven 必需 portfolio_returns，分别可选 target_weights/slippage_bps。factor_research 必需 ic，可选 forward_spread。沿用单图契约。输出路径父目录存在，调用方关闭 Figure | `artifacts` → `metrics.prepare_portfolio_returns`、`portfolio.benchmark.equal_weight_hold`、`portfolio.sizing.simple_quantile_long_short`、`portfolio.sizing.vol_quantile_long_short`、`metrics.ic`、`metrics.rank_ic`、`metrics.prepare_forward_spread` | 无 | 报告 |

### 模型配置与日志（11）

| 接口及真实签名 | 入口条件与数据口径 | 已登记上游（参数 → 接口） | 来源门槛 | Simulator 用途 |
| --- | --- | --- | --- | --- |
| `model.lightgbm.LightGBMParams.booster_params`<br>`(self) -> 'dict'` | LightGBMParams 实例。去除训练轮数与早停轮数，加种子字段 | — | 无 | 离线模型 |
| `model.lambdarank.LambdaRankParams.booster_params`<br>`(self) -> 'dict'` | LambdaRankParams 实例。固定 lambdarank 目标，填种子 | — | 无 | 离线模型 |
| `model.params.ModelParams.to_dict`<br>`(self) -> 'dict'` | 参数 dataclass 实例，asdict 生成独立字典 | — | 无 | 实验记录 |
| `model.params.ModelParams.save`<br>`(self, path: 'Path') -> 'None'` | 参数可 JSON 序列化，Path 的父目录已存在，写 JSON | — | 无 | 实验记录 |
| `model.params.ModelParams.load`<br>`(path: 'Path') -> 'ModelParams'` | 可读 JSON Path，字段匹配 cls 构造器，返回 cls 实例 | `path` → `model.params.ModelParams.save` | 无 | 实验记录 |
| `model.params.TrainLog.save`<br>`(self, path: 'Path') -> 'None'` | TrainLog 实例及可序列化字典，Path 的父目录已存在 | — | 无 | 实验记录 |
| `model.params.TrainLog.load`<br>`(path: 'Path') -> 'TrainLog'` | 可读 JSON Path，含 model/params，可选 metrics | `path` → `model.params.TrainLog.save` | 无 | 实验记录 |
| `tracelog.trace`<br>`(func: 'Callable[..., Any] \| None' = None, *, max_rows: 'int' = 5, log_return: 'bool' = True) -> 'Callable[..., Any]'` | 可调用对象或省略 func，默认最多预览 5 行，返回装饰函数或装饰器 | — | 无 | 诊断，避免日志写入热路径 |
| `tracelog.configure_log`<br>`(path: 'str \| Path') -> 'Path'` | 日志路径可写，可创建父目录，配置进程全局路径 | — | 无 | 诊断 |
| `tracelog.clear_log`<br>`(path: 'str \| Path \| None' = None) -> 'Path'` | 可写日志文件，可创建父目录，直接清空 | `path` → `tracelog.configure_log` | 无 | 维护限定 |
| `tracelog.print_call_chain`<br>`() -> 'str'` | 有调用链时打印并追加日志，返回文本，未追踪则空串 | — | 无 | 诊断 |

### 清单之外的公开入口与类型

- `registry.get_contract(name: str) -> dict` 和 `registry.list_contracts(*, stage: str | None = None, workflow: str | None = None) -> dict` 是 Adapter 的发现接口，无数据来源登记要求。两者上游为静态目录，按字面过滤，查询结果可安全编辑。其他模块的公开重导出沿用同一实现，例如 `quanttoolkit.figure.plot_report`。
- Yahoo 签名中的 `DateLike = str | datetime.date | datetime.datetime`。
- `ModelParams(seed=7)`、`TrainLog(model, params, metrics=<每个实例新字典>)` 为 dataclass 构造器。回归参数额外接收 `objective="regression", metric="l2", learning_rate=0.05, num_leaves=31, min_data_in_leaf=50, feature_fraction=1.0, verbosity=-1, num_boost_round=200, early_stopping_rounds=20`。排序参数为 `LambdaRankParams(seed=7, num_boost_round=100)`。上游是实验配置，返回对应实例，来源无要求，供离线训练及记录。
- `RegressionTree(max_depth=3, min_samples_split=2, min_samples_leaf=1, lambda_l2=0.0)`、`SimpleGBDT(n_trees=100, max_depth=3, lr=0.1, min_samples_split=2, min_samples_leaf=1, lambda_l2=0.0)` 由训练配置构造未拟合实例，来源无要求。适合教学对照，预测前先拟合。`TreeNode()` 只初始化空节点字段，在树实现内使用。
- `db.connection.DB` 与 `db.operation.SQL_DIR` 是路径常量。SQL 视图、独立脚本和私有函数不作为 Adapter 可调用能力。
