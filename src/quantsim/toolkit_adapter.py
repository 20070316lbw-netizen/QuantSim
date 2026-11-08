"""通过 QuantToolKit 公开契约计算策略特征和回放绩效

QuantToolkitAdapter.__init__ 加载并核对公开函数签名及来源契约
capabilities 和 describe 查询能力, for_strategy 将当前 MarketView 绑定到 StrategyAnalytics
StrategyAnalytics.feature 读取可见历史, 计算历史收益、历史波动率或两者的合成分数
Replay 使用本模块的基准、绩效和出图入口, 不向策略暴露这些操作
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from inspect import Parameter, signature
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import pandas as pd

from quantsim.errors import ErrorCode, SimulatorError
from quantsim.market_data import MarketView

if TYPE_CHECKING:
    from matplotlib.figure import Figure

_HISTORICAL_RETURN = "panel.returns.historical_return"
_HISTORY_VOL = "panel.volatility.history_vol"
_COMBINE_SCORES = "factors.combine.combine_scores"
_PREPARE_PANEL = "panel.transfer.to_date_ticker_frame"
_STRATEGY_FEATURES = (_HISTORICAL_RETURN, _HISTORY_VOL, _COMBINE_SCORES)


@dataclass(frozen=True)
class PerformanceMetrics:
    total_return: float | None
    sharpe: float | None
    max_drawdown: float | None
    win_rate: float | None


def replay_metrics(returns: pd.Series) -> PerformanceMetrics:
    """用连续、非重叠的单期简单收益计算绩效, 缺期时区间指标均未定义"""
    if returns.empty or returns.isna().any():
        return PerformanceMetrics(None, None, None, None)
    try:
        from quanttoolkit import metrics
    except (ImportError, OSError) as exc:
        raise SimulatorError(
            ErrorCode.DEPENDENCY_UNAVAILABLE, "无法导入 QuantToolKit 绩效接口"
        ) from exc

    prepared = metrics.prepare_portfolio_returns(returns)
    total = metrics.total_return(returns=prepared)
    sharpe = metrics.sharpe(returns=prepared) if prepared.nunique() > 1 else None
    drawdown = metrics.max_drawdown(returns=prepared)
    wins = metrics.win_rate(returns=prepared)
    return PerformanceMetrics(
        total if isfinite(total) else None,
        sharpe if sharpe is not None and isfinite(sharpe) else None,
        drawdown if isfinite(drawdown) else None,
        wins if isfinite(wins) else None,
    )


def replay_benchmark(prices: pd.DataFrame) -> pd.Series:
    """从列为 date、ticker、close 的完整日历网格计算等权持有基准日收益"""
    try:
        from quanttoolkit.panel.transfer import to_date_ticker_frame
        from quanttoolkit.portfolio.benchmark import equal_weight_hold
    except (ImportError, OSError) as exc:
        raise SimulatorError(
            ErrorCode.DEPENDENCY_UNAVAILABLE, "无法导入 QuantToolKit 基准接口"
        ) from exc

    panel = to_date_ticker_frame(df=prices)
    return equal_weight_hold(prices=panel, price_col="close")


def replay_report(
    returns: pd.Series, benchmark: pd.Series, *, output: Path | None
) -> "Figure":
    """渲染事件回放净值及回撤, 基准连续时在净值图叠加对照"""
    try:
        from matplotlib import pyplot as plt
        from quanttoolkit.figure import plot_report
        from quanttoolkit.metrics import prepare_portfolio_returns
    except (ImportError, OSError) as exc:
        raise SimulatorError(
            ErrorCode.DEPENDENCY_UNAVAILABLE, "无法导入 QuantToolKit 报告接口"
        ) from exc

    figure = plot_report(
        "event_driven", portfolio_returns=prepare_portfolio_returns(returns)
    )
    try:
        if not benchmark.empty and not benchmark.isna().any():
            nav = (1 + benchmark.reindex(returns.index, fill_value=0.0)).cumprod()
            ax = figure.axes[0]
            ax.lines[0].set_label("Portfolio")
            ax.plot(nav.index, nav, label="Equal-weight hold")
            ax.legend()
        if output is not None:
            figure.savefig(output, dpi=150)
    except OSError as exc:
        plt.close(figure)
        raise SimulatorError(ErrorCode.DATA_UNAVAILABLE, "无法保存回放报告") from exc
    except Exception:
        plt.close(figure)
        raise
    return figure


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
    n_periods: int | None
    price_col: str
    requested_instruments: tuple[UUID, ...]
    points: tuple[FeaturePoint, ...]
    window: int | None = None


class QuantToolkitAdapter:
    """查询目录并绑定当前启用的策略计算接口。"""

    def __init__(self) -> None:
        try:
            from quanttoolkit.factors.combine import combine_scores
            from quanttoolkit.factors.zscore import zscore_by_date
            from quanttoolkit.panel.returns import historical_return
            from quanttoolkit.panel.transfer import to_date_ticker_frame
            from quanttoolkit.panel.volatility import history_vol
            from quanttoolkit.registry import get_contract, list_contracts
        except (ImportError, OSError) as exc:
            raise SimulatorError(
                ErrorCode.DEPENDENCY_UNAVAILABLE, "无法导入 QuantToolKit"
            ) from exc

        self._get_contract = get_contract
        self._list_contracts = list_contracts
        self._prepare_panel = to_date_ticker_frame
        self._historical_return = historical_return
        self._history_vol = history_vol
        self._zscore_by_date = zscore_by_date
        self._combine_scores = combine_scores
        capability = _PREPARE_PANEL
        try:
            prepare_spec = get_contract(_PREPARE_PANEL)
            prepare_params = signature(to_date_ticker_frame).parameters
            if (
                tuple(prepare_params) != ("df",)
                or prepare_params["df"].kind != Parameter.KEYWORD_ONLY
                or not prepare_spec["track"]
            ):
                raise ValueError("行情准备入口不匹配")
            for capability, calculate, size in (
                (_HISTORICAL_RETURN, historical_return, "n_periods"),
                (_HISTORY_VOL, history_vol, "window"),
            ):
                price_rule = get_contract(capability)["inputs"]["df"]
                feature_params = signature(calculate).parameters
                if (
                    tuple(feature_params) != ("df", size, "price_col")
                    or any(
                        param.kind != Parameter.KEYWORD_ONLY
                        for param in feature_params.values()
                    )
                    or price_rule["source_policy"] != "required"
                    or price_rule["check"] != "price"
                    or _PREPARE_PANEL not in price_rule["upstream"]
                ):
                    raise ValueError("签名或行情来源规则与策略特征绑定不匹配")
            capability = _COMBINE_SCORES
            factor_rule = get_contract(capability)["inputs"]["z"]
            if (
                tuple(signature(combine_scores).parameters) != ("z", "weights")
                or tuple(signature(zscore_by_date).parameters)
                != ("factors", "date_level")
                or "factors.zscore.zscore_by_date" not in factor_rule["upstream"]
                or factor_rule["source_policy"] == "required"
            ):
                raise ValueError("标准化或合成分数入口不匹配")
        except (KeyError, TypeError, ValueError) as exc:
            raise SimulatorError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"QuantToolKit 的 {capability} 契约与 QuantSim 策略特征绑定不匹配",
                capability=capability,
            ) from exc

    def capabilities(
        self, *, stage: str | None = None, workflow: str | None = None
    ) -> tuple[Capability, ...]:
        return tuple(
            Capability(name, spec, name in _STRATEGY_FEATURES)
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
        return Capability(name, spec, name in _STRATEGY_FEATURES)

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
        """按冻结交易日历计算策略特征, 只返回实际可见行情对应的点

        historical_return 的端点缺失时返回 None, 缺失交易日仍计入期数
        history_vol 的窗口需要连续价格, 合成分数只使用两个特征均有效的证券
        """
        if not self._adapter.describe(name).enabled_for_strategy:
            raise SimulatorError(
                ErrorCode.CAPABILITY_DISABLED, f"策略通道未启用 {name}", capability=name
            )
        if params is not None and not isinstance(params, Mapping):
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST, "params 须为参数映射", capability=name
            )
        params = {} if params is None else params
        defaults = {
            _HISTORICAL_RETURN: {"n_periods": 5},
            _HISTORY_VOL: {"window": 21},
            _COMBINE_SCORES: {"n_periods": 5, "window": 21},
        }[name]
        sizes = {**defaults, **params}
        if set(params) - defaults.keys() or any(
            type(size) is not int or not (2 if key == "window" else 1) <= size < 2**31
            for key, size in sizes.items()
        ):
            raise SimulatorError(
                ErrorCode.INVALID_REQUEST,
                "n_periods 须在 1 至 2³¹−1，window 须在 2 至 2³¹−1，价格列固定为 close",
                capability=name,
            )
        n_periods = sizes.get("n_periods")
        window = sizes.get("window")
        sessions = self._market.sessions(lookback_sessions=lookback_sessions)
        batch = self._market.history(instruments, lookback_sessions=lookback_sessions)
        instrument_by_ticker = {bar.ticker: bar.instrument_id for bar in batch.bars}
        prices = pd.DataFrame(
            ((bar.session_date, bar.ticker, float(bar.close)) for bar in batch.bars),
            columns=["date", "ticker", "close"],
        ).set_index(["date", "ticker"])
        # 给每只证券保留交易日位置, 缺失端点仍为 NaN
        calendar_index = pd.MultiIndex.from_product(
            ([session.session_date for session in sessions], instrument_by_ticker),
            names=prices.index.names,
        )
        frame = prices.reindex(calendar_index).reset_index()
        try:
            # 保留入口返回的对象身份，下游按此检查来源
            panel = self._adapter._prepare_panel(df=frame)
            if n_periods is not None:
                values = self._adapter._historical_return(
                    df=panel, n_periods=min(n_periods, len(sessions)), price_col="close"
                )
            if window is not None:
                volatility = self._adapter._history_vol(
                    df=panel,
                    window=min(window, max(2, len(sessions))),
                    price_col="close",
                )
                if n_periods is None:
                    values = volatility
                else:
                    factors = pd.DataFrame(
                        {"momentum": values, "volatility": volatility}
                    ).dropna()
                    standardized = self._adapter._zscore_by_date(factors).fillna(0.0)
                    values = self._adapter._combine_scores(
                        standardized, weights={"momentum": 1.0, "volatility": -1.0}
                    ).reindex(panel.index)
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

        observed_values = values.loc[panel["close"].notna()]
        points = tuple(
            FeaturePoint(
                session_date.date(),
                instrument_by_ticker[ticker],
                None if pd.isna(value) else float(value),
            )
            for (session_date, ticker), value in observed_values.items()
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
            window,
        )
