# QuantSim

[![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)

![Assisted by Codex](assets/assisted-by-codex.svg)

运行于 macOS 本机的量化交易回放模拟器。当前可在 DuckDB 导入冻结日频行情，查询带历史证券池、披露时点和逐值来源的市场快照，运行第一项 QuantToolKit 策略特征，也可以逐日回放固定策略并从已提交的账本续跑。见 [历史市场导入与查询示例](docs/historical_market.md) 和 [开发流程说明书](docs/development_guide.md)。M0–M5 的早期技术拆解与交付记录见 [实施计划](docs/implementation_plan.md)。

## 安装与测试

使用 Python 3.12 和 uv。在项目目录执行以下命令。

```sh
uv sync --locked
uv run --locked pytest -q
```

QuantToolKit 从相对于项目目录的 `../quant/QuantToolKit` 以可编辑模式安装。修改其中的 Python 源码后，新启动的 QuantSim 进程会直接加载修改后的代码。首次安装需要准备该本机目录，其他依赖按 `uv.lock` 安装。下面的示例和测试使用本地 fixture。

推送和提交 Pull Request 时，GitHub Actions 在 macOS 上安装锁定依赖并运行 Ruff 与测试。CI 将 QuantToolKit 检出到相同的相对路径，固定版本写在 [CI workflow](.github/workflows/ci.yml) 中；更新 Toolkit 依赖时需要同时核对该版本和 `uv.lock`。

## 运行三日回放

在项目目录运行离线示例。它在临时 DuckDB 中导入三日合成行情，从 1,000 USD 开始，按 10 bp 费率模拟买入 10 股和清仓。

```sh
uv run --locked python examples/replay_round_trip.py
```

```text
交易日  现金  股数  权益
2024-07-02  1000.00  0  1000.00
2024-07-03  889.89  10  999.89
2024-07-05  1009.77  0  1009.77
```

需要保留运行时，使用自己的数据库路径。重新打开 `Replay(path, simulation_id)`，用 `next_tick()` 查询待处理步骤。重复调用 `step(seq)` 会读回该步已提交的快照。见 [回放接口](docs/api.md#replay)。

加上 `--report` 可输出费后累计收益、等权持有基准和费用，并保存净值及回撤图。三日样本的策略累计收益为 0.977%，基准累计收益为 20.000%，成交费用为 0.23 USD。

```sh
MPLBACKEND=Agg uv run --locked python examples/replay_round_trip.py --report /tmp/quantsim-replay.png
```

## 运行一条特征

```python
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from quantsim.database import initialize_database
from quantsim.market_data import MarketDataRepository
from quantsim.toolkit_adapter import QuantToolkitAdapter

with TemporaryDirectory() as directory:
    path = Path(directory) / "quant.duckdb"
    initialize_database(path)
    market = MarketDataRepository(path)
    dataset = market.import_batch(Path("tests/fixtures/market.json"))
    view = market.view(dataset.data_version, date(2024, 7, 3))

    adapter = QuantToolkitAdapter()
    result = adapter.for_strategy(market=view).feature(
        "panel.returns.historical_return",
        (UUID(int=1),),
        lookback_sessions=2,
        params={"n_periods": 1},
    )
    for point in result.points:
        print(
            point.session_date, None if point.value is None else round(point.value, 4)
        )
```

输出为 `2024-07-02 None` 和 `2024-07-03 0.1`。前一交易日是预热样本，后一交易日的原始收盘价从 10 变为 11。示例只使用截止时点已发布的行情。行情读取可以保留不可成交的真实观察，撮合资格由独立字段表示。

导入使用 [历史市场格式](docs/historical_market.md#历史数据包)。每批含单市场、单币种、原始日线、带时区的日历和证券上市记录。调整收盘价在 `adjusted_prices` 中提供独立可用时间。相同文件重复导入返回同一批次 ID。

数据库通过 `initialize_database(path)` 初始化并升级到版本 2。版本 1 库可直接升级并保留已有数据，建议升级前备份。升级后可在 `schema_migrations` 查询已应用版本、名称和时间。早于版本 1 且没有匹配版本记录的开发数据库需要重新导入。见 [数据库结构与迁移](docs/database_schema.md#初始化与历史市场表)。
