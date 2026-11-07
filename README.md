# QuantSim

运行于 macOS 本机的量化交易回放模拟器。当前可在 DuckDB 导入冻结日频行情，查询带历史证券池、披露时点和逐值来源的市场快照，并运行第一项 QuantToolKit 策略特征。见 [历史市场导入与查询示例](docs/historical_market.md) 和 [开发流程说明书](docs/development_guide.md)。M0–M5 的早期技术拆解与交付记录见 [实施计划](docs/implementation_plan.md)。

## 安装与测试

使用 Python 3.12 和 uv。在项目目录执行以下命令。

```sh
uv sync --locked
uv run --locked pytest -q
```

QuantToolKit 从相对于项目目录的 `../quant/QuantToolKit` 以可编辑模式安装。修改其中的 Python 源码后，新启动的 QuantSim 进程会直接加载修改后的代码。首次安装需要准备该本机目录，其他依赖按 `uv.lock` 安装。下面的示例和测试使用本地 fixture。

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

起步阶段以当前 [数据库结构](src/quantsim/schema.sql) 为准。遇到之前阶段的 DuckDB 文件，请用新路径初始化，再导入当前格式的原始数据包。
