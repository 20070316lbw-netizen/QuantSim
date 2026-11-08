"""验证独立数据库初始化与迁移

覆盖新库、版本 1 升级、历史一致性和跨版本事务回滚
"""

from importlib.resources import files

import duckdb
import pytest

from quantsim.database import initialize_database
from quantsim.errors import ErrorCode, SimulatorError


def test_initialize_database_can_be_repeated(tmp_path):
    path = tmp_path / "quant.duckdb"

    initialize_database(path)
    with duckdb.connect(path, read_only=True) as connection:
        history = connection.execute(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
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
            "schema_migrations",
            "replay_results",
        } <= tables
        assert connection.execute("SELECT version FROM schema_info").fetchall() == [
            (3,)
        ]
        assert [(version, name) for version, name, _ in history] == [
            (1, "baseline"),
            (2, "migration_history"),
            (3, "replay_results"),
        ]
        assert all(applied_at is not None for _, _, applied_at in history)
        assert (
            connection.execute(
                "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
            == history
        )


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
        connection.execute("UPDATE schema_info SET version = 999")

    with pytest.raises(SimulatorError) as error:
        initialize_database(path)
    assert error.value.code is ErrorCode.INCONSISTENT_STATE
    with duckdb.connect(path, read_only=True) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchall() == [
            (999,)
        ]


def test_upgrade_preserves_data_and_is_repeatable(tmp_path):
    path = tmp_path / "quant.duckdb"
    with duckdb.connect(path) as connection:
        connection.execute(files("quantsim").joinpath("schema.sql").read_text())
        connection.execute("INSERT INTO schema_info (version) VALUES (1)")
        connection.execute(
            "INSERT INTO strategies (strategy_id, name, code_version, config, definition_hash) VALUES ('00000000-0000-0000-0000-000000000001', 'name', 'fixed', '{}', 'hash')"
        )

    initialize_database(path)
    initialize_database(path)
    with duckdb.connect(path, read_only=True) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchall() == [
            (3,)
        ]
        assert connection.execute("SELECT name FROM strategies").fetchall() == [
            ("name",)
        ]
        assert connection.execute(
            "SELECT version, name, applied_at IS NOT NULL "
            "FROM schema_migrations ORDER BY version"
        ).fetchall() == [
            (1, "baseline", False),
            (2, "migration_history", True),
            (3, "replay_results", True),
        ]


def test_incomplete_migration_history_is_rejected(tmp_path):
    path = tmp_path / "quant.duckdb"
    initialize_database(path)
    with duckdb.connect(path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 1")

    with pytest.raises(SimulatorError) as error:
        initialize_database(path)
    assert error.value.code is ErrorCode.INCONSISTENT_STATE


def test_failed_upgrade_rolls_back_schema_and_version(tmp_path, monkeypatch):
    from quantsim import database

    path = tmp_path / "quant.duckdb"
    initialize_database(path)

    def upgrade(connection):
        connection.execute("CREATE TABLE migration_example (value INTEGER)")

    def broken_upgrade(connection):
        connection.execute("INSERT INTO missing_table VALUES (1)")

    expected = database._V3_TABLES | {("main", "migration_example", "BASE TABLE")}
    working_migrations = database._MIGRATIONS + ((4, "example", upgrade, expected),)
    monkeypatch.setattr(
        database,
        "_MIGRATIONS",
        working_migrations + ((5, "broken", broken_upgrade, expected),),
    )
    with pytest.raises(SimulatorError) as error:
        initialize_database(path)
    assert "missing_table" in str(error.value.__cause__)
    with duckdb.connect(path, read_only=True) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchall() == [
            (3,)
        ]
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,)]
        assert ("migration_example",) not in connection.execute(
            "SHOW TABLES"
        ).fetchall()

    monkeypatch.setattr(database, "_MIGRATIONS", working_migrations)
    initialize_database(path)
    with duckdb.connect(path, read_only=True) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchall() == [
            (4,)
        ]
        assert connection.execute(
            "SELECT name, applied_at IS NOT NULL FROM schema_migrations WHERE version = 4"
        ).fetchone() == ("example", True)
