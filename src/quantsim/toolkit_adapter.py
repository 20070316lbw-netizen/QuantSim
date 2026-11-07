"""通过 QuantToolKit 公开契约提供受时点约束的策略能力。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from inspect import Parameter, signature
from uuid import UUID

import pandas as pd

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.market_data import MarketView

_HISTORICAL_RETURN = "panel.returns.historical_return"
_PREPARE_PANEL = "panel.transfer.to_date_ticker_frame"


@dataclass(frozen=True)
class Capability:
    name: str
    contract: dict
    enabled_for_strategy: bool


@dataclass(frozen=True)
class FeaturePoint:
    session_date: date
    instrument_id: UUID
    value: float | None


@dataclass(frozen=True)
class FeatureResult:
    name: str
    data_version: str
    as_of: datetime
    through_session: date
    lookback_sessions: int
    n_periods: int
    price_col: str
    requested_instruments: tuple[UUID, ...]
    points: tuple[FeaturePoint, ...]


class QuantToolkitAdapter:
    """查询目录并绑定当前启用的策略计算接口。"""

    def __init__(self) -> None:
        try:
            from quanttoolkit.panel.returns import historical_return
            from quanttoolkit.panel.transfer import to_date_ticker_frame
            from quanttoolkit.registry import get_contract, list_contracts
        except (ImportError, OSError) as exc:
            raise SimulatorError(
                ErrorCode.DEPENDENCY_UNAVAILABLE, "无法导入 QuantToolKit"
            ) from exc

        self._get_contract = get_contract
        self._list_contracts = list_contracts
        self._prepare_panel = to_date_ticker_frame
        self._historical_return = historical_return
        try:
            prepare_spec = get_contract(_PREPARE_PANEL)
            price_rule = get_contract(_HISTORICAL_RETURN)["inputs"]["df"]
            prepare_params = signature(to_date_ticker_frame).parameters
            return_params = signature(historical_return).parameters
            if (
                tuple(prepare_params) != ("df",)
                or tuple(return_params) != ("df", "n_periods", "price_col")
                or any(
                    param.kind != Parameter.KEYWORD_ONLY
                    for param in (*prepare_params.values(), *return_params.values())
                )
                or not prepare_spec["track"]
                or price_rule["source_policy"] != "required"
                or price_rule["check"] != "price"
                or _PREPARE_PANEL not in price_rule["upstream"]
            ):
                raise ValueError("签名或行情来源规则与策略特征绑定不匹配")
        except (KeyError, TypeError, ValueError) as exc:
            raise SimulatorError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"QuantToolKit 的 {_PREPARE_PANEL} / {_HISTORICAL_RETURN} 契约与 QuantSim 策略特征绑定不匹配",
                capability=_HISTORICAL_RETURN,
            ) from exc

    def capabilities(
        self, *, stage: str | None = None, workflow: str | None = None
    ) -> tuple[Capability, ...]:
        return tuple(
            Capability(name, spec, name == _HISTORICAL_RETURN)
            for name, spec in self._list_contracts(
                stage=stage, workflow=workflow
            ).items()
        )

    def describe(self, name: str) -> Capability:
        try:
            spec = self._get_contract(name)
        except KeyError as exc:
            raise SimulatorError(
                ErrorCode.UNKNOWN_CAPABILITY, f"未知能力 {name}", capability=name
            ) from exc
        return Capability(name, spec, name == _HISTORICAL_RETURN)

    def for_strategy(self, *, market: MarketView) -> "StrategyAnalytics":
        return StrategyAnalytics(self, market)


class StrategyAnalytics:
    """使用当前 MarketView 构造工具箱输入，返回不可修改的特征结果。"""

    def __init__(self, adapter: QuantToolkitAdapter, market: MarketView) -> None:
        self._adapter = adapter
        self._market = market

    def feature(
        self,
        name: str,
        instruments: tuple[UUID, ...],
        *,
        lookback_sessions: int,
        params: Mapping[str, int] | None = None,
    ) -> FeatureResult:
        if not self._adapter.describe(name).enabled_for_strategy:
            raise SimulatorError(
                ErrorCode.CAPABILITY_DISABLED, f"策略通道未启用 {name}", capability=name
            )
        if params is not None and not isinstance(params, Mapping):
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST, "params 须为参数映射", capability=name
            )
        params = {} if params is None else params
        n_periods = params.get("n_periods", 5)
        if (
            set(params) - {"n_periods"}
            or type(n_periods) is not int
            or not 1 <= n_periods < 2**31
        ):
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST,
                "仅允许传入 1 至 2³¹−1 的整数 n_periods，价格列固定为 close",
                capability=name,
            )
        batch = self._market.history(instruments, lookback_sessions=lookback_sessions)
        frame = pd.DataFrame(
            ((bar.session_date, bar.ticker, float(bar.close)) for bar in batch.bars),
            columns=["date", "ticker", "close"],
        )
        # 回看期超过可见日期数时，所有证券仍是缺值，限制底层 shift 分配
        visible_days = len({bar.session_date for bar in batch.bars})
        periods = min(n_periods, max(1, visible_days))
        try:
            # 保留入口返回的对象身份，下游按此检查来源
            panel = self._adapter._prepare_panel(df=frame)
            values = self._adapter._historical_return(
                df=panel, n_periods=periods, price_col="close"
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise SimulatorError(
                ErrorCode.TOOLKIT_INPUT_REJECTED,
                f"QuantToolKit 拒绝 {name} 的输入",
                capability=name,
            ) from exc
        except (ImportError, OSError) as exc:
            raise SimulatorError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"{name} 的依赖不可用",
                capability=name,
            ) from exc
        except Exception as exc:
            raise SimulatorError(
                ErrorCode.TOOLKIT_FAILURE, f"{name} 计算失败", capability=name
            ) from exc

        instrument_by_ticker = {bar.ticker: bar.instrument_id for bar in batch.bars}
        points = tuple(
            FeaturePoint(
                session_date.date(),
                instrument_by_ticker[ticker],
                None if pd.isna(value) else float(value),
            )
            for (session_date, ticker), value in values.items()
        )
        return FeatureResult(
            name,
            batch.data_version,
            batch.as_of,
            self._market.session.session_date,
            lookback_sessions,
            n_periods,
            "close",
            tuple(instruments),
            points,
        )
