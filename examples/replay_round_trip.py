"""从固定三日行情运行离线回放, 可同时输出绩效和净值报告"""

from argparse import ArgumentParser
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from quantsim.database import initialize_database
from quantsim.execution import ExecutionConfig
from quantsim.market_data import MarketDataRepository
from quantsim.replay import Replay, ReplaySpec
from quantsim.strategy import FixedRoundTripStrategy


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="保存净值和回撤图的路径")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    lock_hash = sha256((directory.parent / "uv.lock").read_bytes()).hexdigest()
    with TemporaryDirectory() as temp:
        path = Path(temp) / "round_trip.duckdb"
        initialize_database(path)
        version = MarketDataRepository(path).import_batch(directory / "round_trip.json")
        run = Replay.create(
            path,
            ReplaySpec(
                simulation_id=uuid4(),
                data_version=version.data_version,
                start_session=date(2024, 7, 2),
                end_session=date(2024, 7, 5),
                initial_cash=Decimal(1000),
                execution=ExecutionConfig(Decimal(1), Decimal("0.001")),
                strategy=FixedRoundTripStrategy(UUID(int=1), 10),
                toolkit_revision="local" if args.report else "not_used",
                lock_hash=lock_hash,
            ),
        )
        print("交易日  现金  股数  权益")
        while (tick := run.next_tick()) is not None:
            snapshot = run.step(tick.seq)
            shares = sum(position.quantity for position in snapshot.positions)
            print(
                f"{tick.session_date}  {snapshot.cash:.2f}  {shares}  {snapshot.equity:.2f}"
            )
        if args.report:
            from matplotlib import pyplot as plt

            result = run.performance()
            print(f"策略累计收益 {result.portfolio.total_return:.3%}")
            print(f"等权持有累计收益 {result.benchmark.total_return:.3%}")
            print(f"累计成交费用 {result.total_fees:.2f} {result.currency}")
            figure = run.report(output=args.report)
            plt.close(figure)
            print(f"回放报告 {args.report}")


if __name__ == "__main__":
    main()
