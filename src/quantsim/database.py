"""初始化和核对 QuantSim 独立数据库

initialize_database(path) 在事务中执行迁移、更新版本并记录迁移历史
_check_migration_history 核对已登记的版本和迁移名称
行情仓库及后续回放组件使用初始化后的数据库
"""

from importlib.resources import files
from pathlib import Path

import duckdb

from quantsim.errors import ErrorCode, SimulatorError

_V1_TABLES = {
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
_V2_TABLES = _V1_TABLES | {("main", "schema_migrations", "BASE TABLE")}


def _baseline(connection: duckdb.DuckDBPyConnection) -> None:
    schema = files("quantsim").joinpath("schema.sql")
    connection.execute(schema.read_text(encoding="utf-8"))
    connection.execute("INSERT INTO main.schema_info (version) VALUES (1)")


def _create_migration_history(connection: duckdb.DuckDBPyConnection) -> None:
    migration = files("quantsim").joinpath("migrations", "002_migration_history.sql")
    connection.execute(migration.read_text(encoding="utf-8"))


# 依次登记版本、稳定名称、迁移函数和迁移后的完整表集
_MIGRATIONS = (
    (1, "baseline", _baseline, _V1_TABLES),
    (2, "migration_history", _create_migration_history, _V2_TABLES),
)


def initialize_database(path: Path) -> None:
    """创建或升级独立数据库, 所有待执行迁移在同一事务提交

    path 为 DuckDB 文件路径, 已存在的版本 1 库保留数据
    已有版本 1 库的初始迁移时间无法追溯, 补记为 NULL
    未知版本、表集或历史不匹配及迁移失败均会回滚
    """
    try:
        with duckdb.connect(path) as connection:
            connection.begin()
            tables = _database_tables(connection)
            if not tables:
                version = 0
            else:
                if ("main", "schema_info", "BASE TABLE") not in tables:
                    raise SimulatorError(
                        ErrorCode.INCONSISTENT_STATE,
                        f"数据库 {path} 不是 QuantSim 数据库",
                    )
                versions = connection.execute(
                    "SELECT version FROM main.schema_info"
                ).fetchall()
                if len(versions) != 1 or type(versions[0][0]) is not int:
                    raise SimulatorError(
                        ErrorCode.INCONSISTENT_STATE,
                        f"数据库 {path} 的 schema 版本记录无效",
                    )
                version = versions[0][0]
                known = {number: expected for number, _, _, expected in _MIGRATIONS}
                if version not in known or tables != known[version]:
                    raise SimulatorError(
                        ErrorCode.INCONSISTENT_STATE,
                        f"数据库 {path} 的 schema 版本或表集不匹配",
                    )

            if version >= 2:
                _check_migration_history(connection, version)
            initial_version = version
            for number, name, migrate, expected in _MIGRATIONS:
                if number <= version:
                    continue
                if number != version + 1:
                    raise SimulatorError(
                        ErrorCode.INCONSISTENT_STATE, "数据库迁移版本不连续"
                    )
                migrate(connection)
                if number == 2:
                    connection.execute(
                        """
                        INSERT INTO main.schema_migrations (version, name, applied_at)
                        VALUES (1, 'baseline', CASE WHEN ? THEN current_timestamp END)
                        """,
                        [initial_version == 0],
                    )
                if number > 1:
                    connection.execute(
                        "INSERT INTO main.schema_migrations (version, name) VALUES (?, ?)",
                        [number, name],
                    )
                    connection.execute(
                        "UPDATE main.schema_info SET version = ?", [number]
                    )
                if _database_tables(connection) != expected or connection.execute(
                    "SELECT version FROM main.schema_info"
                ).fetchall() != [(number,)]:
                    raise SimulatorError(
                        ErrorCode.INCONSISTENT_STATE,
                        f"数据库迁移到版本 {number} 后的结构不匹配",
                    )
                version = number
            if version >= 2 and version != initial_version:
                _check_migration_history(connection, version)
            connection.commit()
    except (OSError, duckdb.Error) as exc:
        raise SimulatorError(
            ErrorCode.DATA_UNAVAILABLE, f"无法初始化或升级 QuantSim 数据库 {path}"
        ) from exc


def _check_migration_history(
    connection: duckdb.DuckDBPyConnection, version: int
) -> None:
    expected = [
        (number, name) for number, name, _, _ in _MIGRATIONS if number <= version
    ]
    rows = connection.execute(
        "SELECT version, name FROM main.schema_migrations ORDER BY version"
    ).fetchall()
    if rows != expected:
        raise SimulatorError(ErrorCode.INCONSISTENT_STATE, "数据库迁移历史与版本不匹配")


def _database_tables(
    connection: duckdb.DuckDBPyConnection,
) -> set[tuple[str, str, str]]:
    return set(
        connection.execute(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "WHERE table_catalog = current_database()"
        ).fetchall()
    )
