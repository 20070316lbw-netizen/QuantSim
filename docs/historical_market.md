# M2 历史市场

给定冻结数据版本、交易日、证券池和字段，读取在该时点可见的市场状态。每个有值的字段携带来源和处理记录，行情缺失则保留可解释的状态。首版导入单市场、单币种的日频数据包。

```text
原始输入归档 → 规范化历史行情 → 研究快照
                        └──→ 模拟市场快照
```

模拟市场直接以规范化原始价格为基准。研究层和模拟层各自使用适合其用途的价格。

| 层 | 数据与访问入口 |
| --- | --- |
| Raw Market Data | `market_datasets.payload` 保存提交文件的精确字节，`raw_source(version)` 可读出并核对摘要。导出程序如做过供应商格式转换，应在原始记录附上上游文件、记录 ID 或 URL |
| Normalized Market Data | Decimal 原始 OHLC、股数成交量、独立可用时刻、证券身份和有效区间，`snapshot(..., layer="normalized")` 按当日 cutoff 查询 |
| Research Dataset | `layer="research"` 可选择原始字段、`adjusted_close` 和 `fundamental:<metric>`。默认也使用当日 cutoff。显式 `known_on` 可按后一个交易日的 cutoff 复查过去的行情和修订，这个入口供离线研究 |
| Simulation Market | `simulate_market(normalized_snapshot, shocks)` 产生新的快照，按调用方给定的顺序应用事件，每次将价格半偶舍入到小数点后八位。价格来源追加事件引用和乘数，保留成交量、资格和缺价状态。数据库无写入 |

## 历史数据包

[历史样本](../tests/fixtures/historical_market.json) 展示完整结构。文件包含 `format_version: 2`、`provider`、`source_ref`、市场、币种、交易所时区和 `price_basis: "raw"`。代码、池成员和披露时点应来自提供历史记录的来源。对同一证券保留稳定 UUID 和 `instrument_key`，显示 ticker 走历史代码表。

| 数组 | 必要内容与验证 |
| --- | --- |
| `instruments` | 稳定 ID、provider 对齐键、名称、初始代码、上市日及该消息可用时刻。若已退市，成对填退市日和可用时刻。上市日含当天，退市日为第一天不在上市区间 |
| `sessions` | 显式交易日、带时区的收盘和 cutoff。三者递增，cutoff 在当地同一天且不早于收盘。用来源日历列出实际交易日和提前收盘 |
| `symbols` | 上市时和变更后的代码、有效日、可用时刻。实际有效区间重叠会拒绝。交接消息迟到导致多个活跃证券同一时点都可见为某个代码时，涉及证券标为 `symbol_status="ambiguous"`，保留 UUID 和各自的证据 |
| `pool_changes` | 池名、证券、加入或移除、生效日及可用时刻。两者都到达后采用当时最新的变更。池历史由提供方导入，退市证券留在其历史成员区间 |
| `bars` | 证券、交易日、原始 OHLC、成交量、资格与可用时刻。价格必须为可精确保存的十进制字符串，行情必须落在交易日历与上市区间内。重复键、价格区间异常及收盘前可用的完整日线均拒绝 |
| `session_status` | 可选。独立标记全日 `halted` 或 `no_trade` 及可用时刻。这些状态不能与正成交量并存。允许有来源的零量标记价 |
| `adjusted_prices` | 可选。关联已有原始观察，单独提供调整收盘价、方法和可用时刻。每批使用一种方法，保留后续修订。供应商应按当时可用信息给出时间，避免把今天回填的复权价标为过去已知 |
| `fundamentals` | 可选。证券、指标、报告期末、实际披露时刻、可用时刻、十进制数值和单位。同一指标先按最新可见报告期，再按披露时刻和可用时刻选取。晚到的旧披露不会覆盖更新修订 |

所有时刻转换为 UTC，精度保存到微秒。时区偏移须精确到整秒，超过微秒且会丢失时刻信息的输入会被拒绝。原始行情和辅助记录在一个事务中导入，任何校验或约束失败均不会留下部分版本。完整性报告记录上市区间内的预期证券日、实际行情、明确状态及原因未知的缺行。它度量导入覆盖范围，某日快照还要按其 cutoff 判断数据可见性。

## 查询与解释

先执行 `uv sync --locked`。在项目目录用以下示例验证日期、股票池、字段和来源。

```python
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from quantsim.database import initialize_database
from quantsim.market_data import MarketDataRepository

with TemporaryDirectory() as directory:
    path = Path(directory) / "historical.duckdb"
    initialize_database(path)
    market = MarketDataRepository(path)
    version = market.import_batch(
        Path("tests/fixtures/historical_market.json")
    ).data_version
    snapshot = market.snapshot(
        version,
        date(2024, 7, 5),
        pool="historical",
        fields=("close", "adjusted_close", "fundamental:revenue"),
        layer="research",
    )
    for row in snapshot.rows:
        close = row.field("close")
        print(row.symbol, row.status, close.value, close.lineage.source_ref)
```

样本中，7 月 2 日池内是 `OLD` 与后来退市的 `EXIT`。7 月 5 日是改用 `NEW` 的同一证券，以及复用 `OLD` 代码的新上市证券。`NEW` 的原始收盘是 110，供应商调整收盘是 55。其财报在 7 月 3 日提前收盘的 cutoff 之后披露，7 月 5 日可读到 1250，7 月 8 日显示修订后的 1200。

- `pool` 可为截至快照 cutoff 已公布的池名，或显式 UUID 元组。已公布但尚无生效成员的池返回空快照。用 `pools(version)` 查看归档中的全部池名，用 `fields=()` 仅取证券、状态和时点成员证据。
- 缺行会按当时证据显示 `not_listed`、`delisted`、`halted`、`no_trade`、`unavailable` 或 `missing`。晚于 cutoff 的 bar 在当日快照中也表现为缺行。缺价用 `None` 表示。已知不能成交的情况可返回有状态证据的 `tradable=False`。
- `MarketView.snapshot(...)` 固定绑定交易日的 cutoff，供策略读取可见数据。`MarketDataRepository.snapshot(..., known_on=...)` 的回溯选项只向研究入口提供。
- 每个值的 `lineage` 含版本、原始文件 SHA-256、提供方、记录定位、可用时刻和处理步骤。比如 `fixture:historical-market#bars/3` 指向归档文件的 `bars` 数组第 4 项。财报值另外携带报告期末、披露时刻与单位。
- `versions()`、`version(id)` 和 `raw_source(id)` 用于锁定、核对和检查数据包。版本由导入格式和提交文件字节生成。同文件重导入幂等，修订文件建立新版本，已有研究结果继续引用原版本。实验需同时记录查询日期、cutoff、证券池、字段与层。

## 验证范围

固定样本专门覆盖改码、代码复用、退市、IPO、停牌、缺行、迟到行情、财报披露及修订、独立复权和冲击隔离。样本是合成数据。实际市场覆盖、供应商许可、时点质量及退市证券的处置价格需要随真实数据包验证。当前调整价格由供应商记录导入，拆并股、分红和退市清算对模拟账本的处理属于后续回放工作。
