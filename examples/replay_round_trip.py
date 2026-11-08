"""从固定三日行情运行一次离线回放, 输出每个交易日的现金、股数和权益"""

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
                toolkit_revision="not_used",
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


if __name__ == "__main__":
    main()
