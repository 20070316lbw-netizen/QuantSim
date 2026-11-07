import json
from datetime import UTC, date, datetime
from decimal import ROUND_UP, Decimal, DefaultContext, Inexact
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.simulation_market import PriceShock, simulate_market

FIXTURE = Path(__file__).parent / "fixtures" / "historical_market.json"
A, B, C = (UUID(int=i) for i in (11, 12, 13))
JULY_2 = date(2024, 7, 2)
JULY_3 = date(2024, 7, 3)
JULY_5 = date(2024, 7, 5)
JULY_8 = date(2024, 7, 8)


def test_historical_pool_includes_later_delisted_security(market):
    version = market.import_batch(FIXTURE).data_version

    early = market.snapshot(version, JULY_2, pool="historical", fields=("close",))
    assert [(row.instrument_id, row.symbol) for row in early.rows] == [
        (A, "OLD"),
        (B, "EXIT"),
    ]
    assert early.rows[0].field("close").value == Decimal(100)
    assert early.rows[0].field("close").lineage.source_ref.endswith("#bars/0")

    later = market.snapshot(version, JULY_5, pool="historical", fields=("close",))
    assert [(row.instrument_id, row.symbol) for row in later.rows] == [
        (A, "NEW"),
        (C, "OLD"),
    ]
    assert later.rows[0].membership_lineage is not None

    lifecycle = market.snapshot(version, JULY_5, pool=(B,), fields=("close",))
    assert lifecycle.rows[0].status == "delisted"
    assert lifecycle.rows[0].field("close").value is None


def test_research_projection_respects_publication_and_restatement(market):
    version = market.import_batch(FIXTURE).data_version
    fields = ("close", "adjusted_close", "fundamental:revenue", "tradable")

    early = market.snapshot(
        version, JULY_3, pool=(A, B, C), fields=fields, layer="research"
    )
    assert [row.status for row in early.rows] == ["halted", "missing", "not_listed"]
    assert early.rows[0].field("tradable").value is False
    assert early.rows[0].field("close").value is None
    assert early.rows[0].field("fundamental:revenue").value is None
    assert early.rows[1].field("close").lineage is None

    late = market.snapshot(
        version, JULY_3, pool=(B,), fields=("close",), layer="research", known_on=JULY_5
    )
    assert late.rows[0].field("close").value == Decimal(18)

    current = market.snapshot(
        version, JULY_5, pool=(A,), fields=fields, layer="research"
    )
    row = current.rows[0]
    assert row.field("close").value == Decimal(110)
    assert row.field("adjusted_close").value == Decimal(55)
    assert row.field("adjusted_close").lineage.source_ref.endswith("#adjusted_prices/0")
    assert row.field("adjusted_close").lineage.transformations[-1] == (
        "provider adjustment: provider_split_adjusted"
    )
    filing = row.field("fundamental:revenue")
    assert filing.value == Decimal(1250)
    assert filing.period_end == date(2024, 6, 30)
    assert filing.disclosed_at.isoformat() == "2024-07-03T17:15:00+00:00"
    assert filing.lineage.available_at.isoformat() == "2024-07-03T17:30:00+00:00"
    assert filing.unit == "USD"
    amended = market.snapshot(
        version, JULY_8, pool=(A,), fields=fields, layer="research"
    )
    assert amended.rows[0].field("fundamental:revenue").value == Decimal(1200)

    with pytest.raises(SimulatorError, match="不支持字段"):
        market.snapshot(version, JULY_5, pool=(A,), fields=("adjusted_close",))
    with pytest.raises(SimulatorError, match="交易日历"):
        market.snapshot(version, date(2024, 7, 4), pool=(A,), fields=("close",))


def test_versions_preserve_raw_inputs_and_unexplained_gaps(market, tmp_path):
    old = market.import_batch(FIXTURE).data_version
    new_source = edit_bundle(
        tmp_path, lambda data: data["bars"][0].update({"close": "101"})
    )
    new = market.import_batch(new_source).data_version

    assert market.import_batch(FIXTURE).data_version == old
    assert set(market.versions()) == {old, new}
    info = market.version(old)
    assert info.source_sha256 == sha256(FIXTURE.read_bytes()).hexdigest()
    assert market.raw_source(old) == FIXTURE.read_bytes()
    assert market.raw_source(new) == new_source.read_bytes()
    assert info.integrity.expected_sessions == 8
    assert info.integrity.unexplained_gaps == 1
    for version, expected in [(old, 100), (new, 101)]:
        snapshot = market.snapshot(version, JULY_2, pool=(A,), fields=("close",))
        value = snapshot.rows[0].field("close")
        assert value.value == Decimal(expected)
        assert value.lineage.data_version == version


def test_simulated_price_impact_leaves_historical_snapshot_and_database_intact(market):
    version = market.import_batch(FIXTURE).data_version
    fields = ("open", "high", "low", "close", "volume", "tradable")
    base = market.snapshot(version, JULY_5, pool="historical", fields=fields)
    sim = simulate_market(base, (PriceShock(A, Decimal("0.9"), "scenario:news-7"),))
    assert sim.layer == "simulation"
    assert sim.rows[0].field("open").value == Decimal("94.5")
    assert sim.rows[0].field("close").value == Decimal(99)
    assert sim.rows[0].field("volume") == base.rows[0].field("volume")
    assert sim.rows[1] == base.rows[1]
    assert sim.rows[0].field("close").lineage.transformations[-1] == (
        "scenario:news-7: price × 0.9"
    )
    assert base.rows[0].field("close").value == Decimal(110)
    assert market.snapshot(version, JULY_5, pool="historical", fields=fields) == base

    research = market.snapshot(
        version, JULY_5, pool=(A,), fields=("adjusted_close",), layer="research"
    )
    with pytest.raises(SimulatorError, match="规范化"):
        simulate_market(research, (PriceShock(A, Decimal("0.9"), "scenario:news-7"),))


def test_simulated_prices_ignore_decimal_defaults(market, monkeypatch):
    version = market.import_batch(FIXTURE).data_version
    base = market.snapshot(version, JULY_2, pool=(A,), fields=("close",))
    shock = PriceShock(A, Decimal("1.000000000001"), "scenario:decimal")
    expected = simulate_market(base, (shock,))

    monkeypatch.setattr(DefaultContext, "rounding", ROUND_UP)
    monkeypatch.setitem(DefaultContext.traps, Inexact, True)
    assert expected.rows[0].field("close").value == Decimal("100.00000000")
    assert simulate_market(base, (shock,)) == expected


def edit_bundle(tmp_path, modify):
    bundle = json.loads(FIXTURE.read_bytes())
    modify(bundle)
    target = tmp_path / "changed.json"
    target.write_text(json.dumps(bundle), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda data: data["sessions"][1].update(cutoff_at="2024-07-05T15:00:00Z"),
            "当地同一交易日",
        ),
        (
            lambda data: data["bars"][0].update(
                available_at="2024-07-02T20:00:00.0000009Z"
            ),
            "微秒",
        ),
        (
            lambda data: data["bars"][0].update(
                available_at="2024-07-02T20:00:00-00:00:00.500000"
            ),
            "时区偏移",
        ),
        (
            lambda data: data["bars"][0].update(
                available_at="0001-01-01T00:00:00+01:00"
            ),
            "行情批次格式无效",
        ),
        (lambda data: data["bars"][0].update(adj_close="50"), "独立可用时间"),
        (lambda data: data["bars"][0].update(session_date="2024-07-04"), "2024"),
        (
            lambda data: data["fundamentals"][0].update(
                available_at="2024-07-03T17:00:00Z"
            ),
            "财报时序",
        ),
        (lambda data: data["symbols"][1].update(symbol="OLD"), "分配给多个"),
        (
            lambda data: data["pool_changes"].append(data["pool_changes"][0]),
            "数据库约束",
        ),
    ],
)
def test_import_rejects_inconsistent_batches_atomically(
    market, tmp_path, mutate, message
):
    valid = market.import_batch(FIXTURE).data_version
    source = edit_bundle(tmp_path, mutate)
    with pytest.raises(SimulatorError, match=message) as error:
        market.import_batch(source)
    assert error.value.code == "INVALID_REQUEST"
    assert market.versions() == (valid,)


def test_import_rejects_positive_volume_during_full_day_halt(market, tmp_path):
    def contradictory_bar(data):
        data["bars"].append(
            {
                **data["bars"][0],
                "session_date": "2024-07-03",
                "available_at": "2024-07-03T17:00:00Z",
                "tradable": False,
            }
        )

    with pytest.raises(SimulatorError, match="正成交量冲突"):
        market.import_batch(edit_bundle(tmp_path, contradictory_bar))
    assert market.versions() == ()


def test_price_precision_survives_long_trailing_zeroes(market, tmp_path):
    def update_price(data):
        bar = data["bars"][0]
        for field in ("open", "high", "low", "close"):
            bar[field] = (
                "123456789012.12345678000000000000000000000000000000000000000000"
            )

    version = market.import_batch(edit_bundle(tmp_path, update_price)).data_version
    snapshot = market.snapshot(version, JULY_2, pool=(A,), fields=("close",))
    assert snapshot.rows[0].field("close").value == Decimal("123456789012.12345678")


def test_bar_with_hour_minute_offset_is_visible_at_exact_cutoff(market, tmp_path):
    source = edit_bundle(
        tmp_path,
        lambda data: data["bars"][0].update(available_at="2024-07-02T16:00:00-04:00"),
    )
    version = market.import_batch(source).data_version
    snapshot = market.view(version, JULY_2).snapshot(pool=(A,), fields=("close",))
    close = snapshot.rows[0].field("close")
    assert close.value == Decimal(100)
    assert close.lineage.available_at == datetime(2024, 7, 2, 20, tzinfo=UTC)


@pytest.mark.parametrize("revised_first", [True, False])
def test_batch_import_order_is_independent_of_security_display_name(
    market, tmp_path, revised_first
):
    def revise_display(data):
        data["instruments"][0].update(name="Revised Name", symbol="EARLY")
        data["symbols"][0]["symbol"] = "EARLY"

    revised = edit_bundle(tmp_path, revise_display)
    sources = (revised, FIXTURE) if revised_first else (FIXTURE, revised)
    versions = {source: market.import_batch(source).data_version for source in sources}

    for source, symbol in ((FIXTURE, "OLD"), (revised, "EARLY")):
        version = versions[source]
        assert market.raw_source(version) == source.read_bytes()
        view = market.view(version, JULY_2)
        (bar,) = view.history((A,), lookback_sessions=1).bars
        assert bar.ticker == "fixture-11"
        assert view.snapshot(pool=(A,), fields=()).rows[0].symbol == symbol


def test_recycled_symbol_is_withheld_when_the_change_notice_arrives_late(
    market, tmp_path
):
    def delay_rename(data):
        data["symbols"][1]["available_at"] = "2024-07-08T20:00:00Z"

    version = market.import_batch(edit_bundle(tmp_path, delay_rename)).data_version
    snapshot = market.snapshot(version, JULY_5, pool=(C,), fields=("close",))
    assert snapshot.rows[0].symbol is None
    assert snapshot.rows[0].symbol_status == "ambiguous"
    assert snapshot.rows[0].symbol_lineage.source_ref.endswith("#symbols/3")
    assert snapshot.rows[0].field("close").value == Decimal(11)

    resolved = market.snapshot(version, JULY_8, pool="historical", fields=())
    assert [(row.instrument_id, row.symbol) for row in resolved.rows] == [
        (A, "NEW"),
        (C, "OLD"),
    ]


def test_late_older_filing_cannot_override_a_visible_amendment(market, tmp_path):
    def delay_old_filing(data):
        data["fundamentals"][0]["available_at"] = "2024-07-08T15:30:00Z"

    version = market.import_batch(edit_bundle(tmp_path, delay_old_filing)).data_version
    fields = ("fundamental:revenue",)
    early = market.view(version, JULY_5).snapshot(
        pool=(A,), fields=fields, layer="research"
    )
    assert early.rows[0].field(fields[0]).value is None
    later = market.view(version, JULY_8).snapshot(
        pool=(A,), fields=fields, layer="research"
    )
    assert later.rows[0].field(fields[0]).value == Decimal(1200)


def test_pool_change_needs_both_effective_date_and_publication(market, tmp_path):
    def delay_membership(data):
        data["pool_changes"][3]["available_at"] = "2024-07-08T16:00:00Z"

    version = market.import_batch(edit_bundle(tmp_path, delay_membership)).data_version
    assert market.pools(version) == ("historical",)
    early = market.view(version, JULY_5).snapshot(pool="historical", fields=())
    assert tuple(row.instrument_id for row in early.rows) == (A,)
    later = market.view(version, JULY_8).snapshot(pool="historical", fields=())
    assert tuple(row.instrument_id for row in later.rows) == (A, C)
    with pytest.raises(SimulatorError, match="只接受研究层"):
        market.snapshot(version, JULY_5, pool="historical", fields=(), known_on=JULY_8)


def test_strategy_cannot_distinguish_unpublished_and_unknown_pools(market, tmp_path):
    def add_pool(data):
        data["pool_changes"].append(
            {
                **data["pool_changes"][3],
                "pool_name": "new",
                "available_at": "2024-07-03T16:00:00Z",
            }
        )

    version = market.import_batch(edit_bundle(tmp_path, add_pool)).data_version
    assert market.pools(version) == ("historical", "new")

    early = market.view(version, JULY_2)
    for pool in ("new", "unknown"):
        with pytest.raises(SimulatorError, match="未知证券池") as error:
            early.snapshot(pool=pool, fields=())
        assert error.value.code is ErrorCode.INVALID_REQUEST

    announced = market.view(version, JULY_3).snapshot(pool="new", fields=())
    assert announced.rows == ()
    effective = market.view(version, JULY_5).snapshot(pool="new", fields=())
    assert tuple(row.instrument_id for row in effective.rows) == (C,)
