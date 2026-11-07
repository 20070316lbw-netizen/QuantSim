import duckdb
import pytest

from quantsim.database import initialize_database
from quantsim.errors import ErrorCode, SimulatorError


def test_initialize_database_can_be_repeated(tmp_path):
    path = tmp_path / "quant.duckdb"

    initialize_database(path)
    initialize_database(path)

    with duckdb.connect(path, read_only=True) as connection:
        tables = {name for (name,) in connection.execute("show tables").fetchall()}
        assert {
            "instruments",
            "market_bars",
            "market_datasets",
            "market_listings",
            "market_fundamentals",
            "simulations",
            "events",
            "strategies",
            "orders",
            "fills",
            "cash_ledger",
            "positions_daily",
            "experiments",
            "backtest_runs",
        } <= tables
        assert connection.execute("SELECT version FROM schema_info").fetchall() == [
            (1,)
        ]


def test_initialize_database_refuses_to_modify_another_database(tmp_path):
    path = tmp_path / "research.duckdb"
    with duckdb.connect(path) as connection:
        connection.execute("CREATE TABLE prices (close DOUBLE)")

    with pytest.raises(SimulatorError) as error:
        initialize_database(path)
    assert error.value.code is ErrorCode.INCONSISTENT_STATE
    with duckdb.connect(path, read_only=True) as connection:
        assert connection.execute("SHOW TABLES").fetchall() == [("prices",)]


def test_initialize_database_refuses_a_different_schema_version(tmp_path):
    path = tmp_path / "quant.duckdb"
    initialize_database(path)
    with duckdb.connect(path) as connection:
        connection.execute("UPDATE schema_info SET version = 2")

    with pytest.raises(SimulatorError) as error:
        initialize_database(path)
    assert error.value.code is ErrorCode.INCONSISTENT_STATE
    with duckdb.connect(path, read_only=True) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchall() == [
            (2,)
        ]
