from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import product
import json
from pathlib import Path
import re
from typing import Any, Protocol
import tomllib

BACKTEST_RESEARCH_BOUNDARY = "RESEARCH ONLY — TradingView MCP backtests; no broker orders, no ledger trades"
BACKTEST_STRATEGIES = ("rsi", "bollinger", "macd", "ema_cross", "supertrend", "donchian")
BACKTEST_PERIODS = ("1mo", "3mo", "6mo", "1y", "2y")
BACKTEST_INTERVALS = ("1d", "1h")


class MCPToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(slots=True)
class BacktestFarmConfig:
    name: str = "tradingview-backtest-farm"
    description: str = "Matrix backtests for TradingView MCP strategy families."
    symbols: list[str] = field(default_factory=lambda: ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "BTC-USD", "ETH-USD"])
    strategies: list[str] = field(default_factory=lambda: list(BACKTEST_STRATEGIES))
    periods: list[str] = field(default_factory=lambda: ["1y", "2y"])
    intervals: list[str] = field(default_factory=lambda: ["1d"])
    initial_capital_values: list[float] = field(default_factory=lambda: [10_000.0])
    commission_pct_values: list[float] = field(default_factory=lambda: [0.1])
    slippage_pct_values: list[float] = field(default_factory=lambda: [0.05])
    include_trade_log: bool = False
    include_equity_curve: bool = False
    max_runs: int | None = 96
    output_path: Path = Path("data/research/backtest-farm-results.jsonl")


@dataclass(frozen=True, slots=True)
class BacktestExperiment:
    symbol: str
    strategy: str
    period: str
    interval: str
    initial_capital: float = 10_000.0
    commission_pct: float = 0.1
    slippage_pct: float = 0.05


def _list_from_value(value: object, *, upper: bool = False, lower: bool = False) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [part for part in re.split(r"[,\n]", value) if part.strip()]
    elif isinstance(value, list):
        values = value
    else:
        values = [value]
    result: list[str] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        if upper:
            text = text.upper()
        if lower:
            text = text.lower()
        result.append(text)
    return result


def _float_list_from_value(value: object, default: list[float]) -> list[float]:
    if value is None:
        return list(default)
    values = value if isinstance(value, list) else [value]
    result: list[float] = []
    for raw in values:
        try:
            result.append(float(raw))
        except (TypeError, ValueError):
            continue
    return result or list(default)


def _normalize_strategy(value: str) -> str:
    strategy = value.strip().lower().replace("-", "_")
    aliases = {
        "all": "all",
        "ema": "ema_cross",
        "ema_cross": "ema_cross",
        "super_trend": "supertrend",
        "supertrend": "supertrend",
        "donchian_channel": "donchian",
    }
    return aliases.get(strategy, strategy)


def _normalize_period(value: str) -> str:
    period = value.strip().lower()
    aliases = {"1month": "1mo", "3months": "3mo", "6months": "6mo", "1year": "1y", "2years": "2y"}
    return aliases.get(period, period)


def _normalize_interval(value: str) -> str:
    interval = value.strip().lower()
    aliases = {"daily": "1d", "day": "1d", "hourly": "1h", "hour": "1h"}
    return aliases.get(interval, interval)


def _valid_strategies(values: list[str]) -> list[str]:
    normalized = [_normalize_strategy(value) for value in values]
    if not normalized or "all" in normalized:
        return list(BACKTEST_STRATEGIES)
    return [strategy for strategy in normalized if strategy in BACKTEST_STRATEGIES] or list(BACKTEST_STRATEGIES)


def _valid_periods(values: list[str]) -> list[str]:
    periods = [_normalize_period(value) for value in values]
    return [period for period in periods if period in BACKTEST_PERIODS] or ["1y"]


def _valid_intervals(values: list[str]) -> list[str]:
    intervals = [_normalize_interval(value) for value in values]
    return [interval for interval in intervals if interval in BACKTEST_INTERVALS] or ["1d"]


def load_backtest_farm_config(path: str | Path) -> BacktestFarmConfig:
    path = Path(path)
    data = tomllib.loads(path.read_text())
    output_path = Path(str(data.get("output_path", "data/research/backtest-farm-results.jsonl")))
    return BacktestFarmConfig(
        name=str(data.get("name", "tradingview-backtest-farm")),
        description=str(data.get("description", "Matrix backtests for TradingView MCP strategy families.")),
        symbols=_list_from_value(data.get("symbols", []), upper=True) or ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "BTC-USD", "ETH-USD"],
        strategies=_valid_strategies(_list_from_value(data.get("strategies", ["all"]))),
        periods=_valid_periods(_list_from_value(data.get("periods", ["1y", "2y"]))),
        intervals=_valid_intervals(_list_from_value(data.get("intervals", ["1d"]))),
        initial_capital_values=_float_list_from_value(data.get("initial_capital_values", data.get("initial_capital")), [10_000.0]),
        commission_pct_values=_float_list_from_value(data.get("commission_pct_values", data.get("commission_pct")), [0.1]),
        slippage_pct_values=_float_list_from_value(data.get("slippage_pct_values", data.get("slippage_pct")), [0.05]),
        include_trade_log=bool(data.get("include_trade_log", False)),
        include_equity_curve=bool(data.get("include_equity_curve", False)),
        max_runs=(None if data.get("max_runs") in {None, 0, "0", "none", "None"} else max(1, int(data.get("max_runs", 96)))),
        output_path=output_path,
    )


def build_backtest_experiments(config: BacktestFarmConfig) -> list[BacktestExperiment]:
    experiments: list[BacktestExperiment] = []
    for symbol, strategy, period, interval, initial_capital, commission_pct, slippage_pct in product(
        config.symbols,
        _valid_strategies(config.strategies),
        _valid_periods(config.periods),
        _valid_intervals(config.intervals),
        config.initial_capital_values,
        config.commission_pct_values,
        config.slippage_pct_values,
    ):
        experiments.append(
            BacktestExperiment(
                symbol=symbol,
                strategy=strategy,
                period=period,
                interval=interval,
                initial_capital=float(initial_capital),
                commission_pct=float(commission_pct),
                slippage_pct=float(slippage_pct),
            )
        )
        if config.max_runs is not None and len(experiments) >= config.max_runs:
            break
    return experiments


def _find_number(payload: Any, names: tuple[str, ...]) -> float | None:
    if isinstance(payload, dict):
        lowered = {str(key).lower().replace(" ", "_").replace("-", "_"): value for key, value in payload.items()}
        for name in names:
            key = name.lower().replace(" ", "_").replace("-", "_")
            if key in lowered:
                try:
                    return float(str(lowered[key]).replace("%", "").replace("+", ""))
                except (TypeError, ValueError):
                    pass
        for value in payload.values():
            found = _find_number(value, names)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_number(value, names)
            if found is not None:
                return found
    return None


def summarize_backtest_payload(experiment: BacktestExperiment, payload: dict[str, Any], *, error: str | None = None) -> dict[str, Any]:
    total_return_pct = _find_number(payload, ("total_return_pct", "total_return", "return_pct", "return_%", "profit_pct"))
    sharpe_ratio = _find_number(payload, ("sharpe_ratio", "sharpe"))
    calmar_ratio = _find_number(payload, ("calmar_ratio", "calmar"))
    max_drawdown_pct = _find_number(payload, ("max_drawdown_pct", "max_drawdown", "drawdown_pct"))
    profit_factor = _find_number(payload, ("profit_factor",))
    expectancy = _find_number(payload, ("expectancy",))
    win_rate = _find_number(payload, ("win_rate", "win_rate_pct", "win_%"))
    buy_hold_return_pct = _find_number(payload, ("buy_hold_return_pct", "buy_and_hold_return", "vs_buy_and_hold"))
    return {
        "research_only": True,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": experiment.symbol,
        "strategy": experiment.strategy,
        "period": experiment.period,
        "interval": experiment.interval,
        "initial_capital": experiment.initial_capital,
        "commission_pct": experiment.commission_pct,
        "slippage_pct": experiment.slippage_pct,
        "total_return_pct": total_return_pct,
        "sharpe_ratio": sharpe_ratio,
        "calmar_ratio": calmar_ratio,
        "max_drawdown_pct": max_drawdown_pct,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "win_rate": win_rate,
        "buy_hold_return_pct": buy_hold_return_pct,
        "error": error,
        "raw": payload,
    }


def _rank_key(row: dict[str, Any]) -> tuple[float, float, float, float, str, str]:
    total_return = float(row.get("total_return_pct") or -1_000_000)
    sharpe = float(row.get("sharpe_ratio") or -1_000_000)
    calmar = float(row.get("calmar_ratio") or -1_000_000)
    drawdown = abs(float(row.get("max_drawdown_pct") or 1_000_000))
    return (-total_return, -sharpe, -calmar, drawdown, str(row.get("symbol", "")), str(row.get("strategy", "")))


async def run_backtest_farm(config: BacktestFarmConfig, provider: MCPToolCaller) -> dict[str, Any]:
    experiments = build_backtest_experiments(config)
    config.output_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with config.output_path.open("a", encoding="utf-8") as handle:
        for experiment in experiments:
            arguments = {
                "symbol": experiment.symbol,
                "strategy": experiment.strategy,
                "period": experiment.period,
                "initial_capital": int(experiment.initial_capital) if experiment.initial_capital.is_integer() else experiment.initial_capital,
                "commission_pct": experiment.commission_pct,
                "slippage_pct": experiment.slippage_pct,
                "interval": experiment.interval,
                "include_trade_log": config.include_trade_log,
                "include_equity_curve": config.include_equity_curve,
            }
            try:
                payload = await provider.call_tool("backtest_strategy", arguments)
                payload_error = payload.get("error") if isinstance(payload, dict) else None
                row = summarize_backtest_payload(experiment, payload, error=str(payload_error) if payload_error else None)
                if payload_error:
                    errors.append({"symbol": experiment.symbol, "strategy": experiment.strategy, "error": str(payload_error)})
            except Exception as exc:  # pragma: no cover - defensive for upstream MCP failures
                error = f"{type(exc).__name__}: {exc}"
                row = summarize_backtest_payload(experiment, {}, error=error)
                errors.append({"symbol": experiment.symbol, "strategy": experiment.strategy, "error": error})
            rows.append(row)
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            handle.flush()
    leaderboard = [row for row in rows if row.get("error") is None]
    leaderboard = sorted(leaderboard, key=_rank_key)
    for index, row in enumerate(leaderboard, start=1):
        row["rank"] = index
    return {
        "boundary": BACKTEST_RESEARCH_BOUNDARY,
        "name": config.name,
        "description": config.description,
        "experiment_count": len(experiments),
        "completed_count": len(rows) - len(errors),
        "error_count": len(errors),
        "output_path": str(config.output_path),
        "leaderboard": leaderboard,
        "errors": errors,
    }


def _fmt_pct(value: object, default: str = "n/a") -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return default


def _fmt_num(value: object, digits: int = 2, default: str = "n/a") -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return default


def format_backtest_report(result: dict[str, Any]) -> str:
    lines = [
        str(result.get("boundary") or BACKTEST_RESEARCH_BOUNDARY),
        f"Backtest farm: {result.get('name', 'tradingview-backtest-farm')} ({result.get('experiment_count', 0)} experiments)",
        f"Output: {result.get('output_path', 'n/a')}",
    ]
    if result.get("errors"):
        lines.append(f"Errors: {len(result['errors'])}")
    rows = result.get("leaderboard") or []
    if not rows:
        lines.append("No successful backtests yet.")
        return "\n".join(lines)
    lines.append("Leaderboard by total return, then Sharpe/Calmar:")
    lines.append("rank symbol     strategy     period int return   sharpe calmar win%  drawdown")
    for index, row in enumerate(rows[:25], start=1):
        rank = int(row.get("rank") or index)
        lines.append(
            f"{rank:>4} "
            f"{str(row.get('symbol', ''))[:10]:10} "
            f"{str(row.get('strategy', ''))[:12]:12} "
            f"{str(row.get('period', ''))[:6]:6} "
            f"{str(row.get('interval', ''))[:3]:3} "
            f"{_fmt_pct(row.get('total_return_pct')):>8} "
            f"{_fmt_num(row.get('sharpe_ratio')):>6} "
            f"{_fmt_num(row.get('calmar_ratio')):>6} "
            f"{_fmt_num(row.get('win_rate'), 1):>5} "
            f"{_fmt_pct(row.get('max_drawdown_pct')):>9}"
        )
    return "\n".join(lines)


def config_to_jsonable(config: BacktestFarmConfig) -> dict[str, Any]:
    data = asdict(config)
    data["output_path"] = str(config.output_path)
    return data
