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
