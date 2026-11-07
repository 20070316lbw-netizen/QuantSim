"""提供共享的测试环境

market fixture 调用 initialize_database 创建临时库并返回 MarketDataRepository, 供各测试按需导入数据
"""

from pathlib import Path

import pytest

from quantsim.database import initialize_database
from quantsim.market_data import MarketDataRepository

FIXTURE = Path(__file__).parent / "fixtures" / "market.json"


@pytest.fixture
def market(tmp_path):
    path = tmp_path / "quant.duckdb"
    initialize_database(path)
    return MarketDataRepository(path)
