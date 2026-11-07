"""验证 QuantToolKit 策略适配边界

test_strategy_feature_reuses_toolkit_with_visible_market_data 对照可见行情和特征结果
test_strategy_rejects_disabled_capabilities_and_invalid_parameters 检查能力及参数限制
test_toolkit_public_price_contract_survives_adapter_integration 检查公开来源契约
test_strategy_error_preserves_toolkit_source_rejection 检查异常链, 内部 copy_prepared_panel 模拟来源丢失
test_feature_without_any_visible_bar_has_no_points 检查空行情结果
test_historical_return_counts_sessions_with_missing_bars 检查缺行时的交易日期数
"""

import json
from datetime import date, datetime
from uuid import UUID

import pandas as pd
import pytest
from conftest import FIXTURE
from quanttoolkit.panel.returns import historical_return
from quanttoolkit.panel.transfer import prepare_price_panel
from quanttoolkit.registry import list_contracts

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.toolkit_adapter import QuantToolkitAdapter

FEATURE = "panel.returns.historical_return"
A = UUID(int=1)
B = UUID(int=2)


def test_strategy_feature_reuses_toolkit_with_visible_market_data(market):
    version = market.import_batch(FIXTURE).data_version
    adapter = QuantToolkitAdapter()
    assert {cap.name for cap in adapter.capabilities()} == set(list_contracts())
    assert adapter.describe(FEATURE).enabled_for_strategy
    assert (
        adapter.describe(FEATURE).contract["inputs"]["df"]["source_policy"]
        == "required"
    )
    assert FEATURE in {cap.name for cap in adapter.capabilities(stage="特征计算")}

    view = market.view(version, date(2024, 7, 3))
    result = adapter.for_strategy(market=view).feature(
        FEATURE, (A, B), lookback_sessions=2, params={"n_periods": 1}
    )
    assert result.data_version == version
    assert result.as_of == datetime.fromisoformat("2024-07-03T17:00:00Z")
    assert [(point.session_date, point.instrument_id) for point in result.points] == [
        (date(2024, 7, 2), A),
        (date(2024, 7, 3), A),
    ]
    assert result.points[0].value is None
    assert result.points[1].value == pytest.approx(0.10)


@pytest.mark.parametrize(
    ("name", "params", "code"),
    [
        ("metrics.ic", {}, ErrorCode.CAPABILITY_DISABLED),
        ("panel.returns.future_returns", {}, ErrorCode.CAPABILITY_DISABLED),
        ("unknown", {}, ErrorCode.UNKNOWN_CAPABILITY),
        (FEATURE, {"n_periods": -1}, ErrorCode.INVALID_REQUEST),
        (FEATURE, {"n_periods": True}, ErrorCode.INVALID_REQUEST),
        (FEATURE, {"n_periods": 2**31}, ErrorCode.INVALID_REQUEST),
        (FEATURE, [], ErrorCode.INVALID_REQUEST),
        (FEATURE, {"price_col": "invalid"}, ErrorCode.INVALID_REQUEST),
    ],
)
def test_strategy_rejects_disabled_capabilities_and_invalid_parameters(
    market, name, params, code
):
    version = market.import_batch(FIXTURE).data_version
    strategy = QuantToolkitAdapter().for_strategy(
        market=market.view(version, date(2024, 7, 3))
    )
    with pytest.raises(SimulatorError) as error:
        strategy.feature(name, (A,), lookback_sessions=2, params=params)
    assert error.value.code is code
    assert error.value.capability == name


def test_toolkit_public_price_contract_survives_adapter_integration():
    index = pd.MultiIndex.from_product(
        [pd.date_range("2024-07-02", periods=2), ["A"]], names=["date", "ticker"]
    )
    prepared = prepare_price_panel(pd.DataFrame({"close": [10.0, 11.0]}, index=index))
    with pytest.raises(ValueError, match="缺少上游"):
        historical_return(df=prepared.copy(), n_periods=1, price_col="close")
    with pytest.raises(KeyError, match="缺少价格列"):
        historical_return(df=prepared, n_periods=1, price_col="missing")
    assert historical_return(df=prepared, n_periods=1, price_col="close").iloc[
        1
    ] == pytest.approx(0.10)


def test_strategy_error_preserves_toolkit_source_rejection(market, monkeypatch):
    from functools import wraps

    from quanttoolkit.panel import transfer

    original = transfer.to_date_ticker_frame

    @wraps(original)
    def copy_prepared_panel(*, df):
        return original(df=df).copy()

    monkeypatch.setattr(transfer, "to_date_ticker_frame", copy_prepared_panel)
    version = market.import_batch(FIXTURE).data_version
    strategy = QuantToolkitAdapter().for_strategy(
        market=market.view(version, date(2024, 7, 3))
    )

    with pytest.raises(SimulatorError) as error:
        strategy.feature(FEATURE, (A,), lookback_sessions=2)
    assert error.value.code is ErrorCode.TOOLKIT_INPUT_REJECTED
    assert error.value.capability == FEATURE
    assert isinstance(error.value.__cause__, ValueError)
    assert "缺少上游" in str(error.value.__cause__)


def test_feature_without_any_visible_bar_has_no_points(market):
    version = market.import_batch(FIXTURE).data_version
    strategy = QuantToolkitAdapter().for_strategy(
        market=market.view(version, date(2024, 7, 3))
    )

    result = strategy.feature(FEATURE, (B,), lookback_sessions=2)
    assert result.points == ()
    assert result.requested_instruments == (B,)


@pytest.mark.parametrize(("periods", "expected"), [(1, None), (2, 1.2)])
def test_historical_return_counts_sessions_with_missing_bars(
    market, tmp_path, periods, expected
):
    data = json.loads(FIXTURE.read_text())
    data["bars"] = [
        bar
        for bar in data["bars"]
        if (bar["instrument_key"], bar["session_date"]) != ("A", "2024-07-03")
    ]
    path = tmp_path / "missing.json"
    path.write_text(json.dumps(data))
    version = market.import_batch(path).data_version
    strategy = QuantToolkitAdapter().for_strategy(
        market=market.view(version, date(2024, 7, 5))
    )
    result = strategy.feature(
        FEATURE, (A, B), lookback_sessions=3, params={"n_periods": periods}
    )
    points = [point for point in result.points if point.instrument_id == A]
    assert [point.session_date for point in points] == [
        date(2024, 7, 2),
        date(2024, 7, 5),
    ]
    assert points[0].value is None
    assert points[1].value == (None if expected is None else pytest.approx(expected))
