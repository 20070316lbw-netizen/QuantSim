"""初始化 QuantSim 独立的 DuckDB 文件。"""

from importlib.resources import files
from pathlib import Path

import duckdb

from quantsim.errors import ErrorCode, SimulatorError

_SCHEMA_VERSION = 1
_TABLES = {
    ("main", name, "BASE TABLE")
    for name in (
        "schema_info",
        "market_datasets",
        "instruments",
        "market_listings",
        "market_symbols",
        "market_pool_changes",
        "market_session_status",
        "market_adjusted_prices",
        "market_fundamentals",
        "market_bars",
        "strategies",
        "simulations",
        "orders",
        "events",
        "fills",
        "cash_ledger",
        "positions_daily",
        "experiments",
        "backtest_runs",
    )
}


def initialize_database(path: Path) -> None:
    """原子建立当前 schema，重复启动时核对表集和版本。"""
    try:
        with duckdb.connect(path) as connection:
            connection.begin()
            tables = set(
                connection.execute(
                    "SELECT table_schema, table_name, table_type FROM information_schema.tables "
                    "WHERE table_catalog = current_database()"
                ).fetchall()
            )
            if not tables:
                schema = files("quantsim").joinpath("schema.sql")
                connection.execute(schema.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO main.schema_info (version) VALUES (?)",
                    [_SCHEMA_VERSION],
                )
            elif tables != _TABLES or connection.execute(
                "SELECT version FROM main.schema_info"
            ).fetchall() != [(_SCHEMA_VERSION,)]:
                raise SimulatorError(
                    ErrorCode.INCONSISTENT_STATE,
                    f"数据库 {path} 与当前 QuantSim schema 不匹配，请使用新的独立数据库路径",
                )
            connection.commit()
    except (OSError, duckdb.Error) as exc:
        raise SimulatorError(
            ErrorCode.DATA_UNAVAILABLE, f"无法初始化 QuantSim 数据库 {path}"
        ) from exc
