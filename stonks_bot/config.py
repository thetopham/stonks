from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib

from .models import WatchItem


@dataclass(slots=True)
class ExecutionConfig:
    dry_run: bool = True
    live_trading_enabled: bool = False
    scan_interval_seconds: int = 900


@dataclass(slots=True)
class StrategyConfig:
    entry_score: float = 65.0
    exit_score: float = 35.0
    max_rsi_for_entry: float = 70.0
    stop_loss_pct: float = 0.07
    take_profit_pct: float = 0.15


@dataclass(slots=True)
class SelectionConfig:
    mode: str = "ranked"
    preview_top: int = 10


@dataclass(slots=True)
class ScreenerConfig:
    enabled: bool = False
    # curated = local watchlist/curated baskets only; mcp = dynamic MCP scans + watchlist;
    # hybrid = dynamic MCP scans + configured local baskets.
    source: str = "curated"
    universes: list[str] = field(default_factory=lambda: ["watchlist"])
    exchanges: list[str] = field(default_factory=lambda: ["NASDAQ", "NYSE"])
    dynamic_sources: list[str] = field(
        default_factory=lambda: ["rating_strong_buy", "rating_buy", "volume_breakout", "smart_volume", "top_gainers"]
    )
    per_source_limit: int = 50
    max_candidates: int = 100
    exclude_symbols: list[str] = field(default_factory=list)


@dataclass(slots=True)
class OptimizerConfig:
    enabled: bool = True
    cash_reserve_pct: float = 0.05
    max_new_buys_per_scan: int = 3
    min_position_notional: float = 25.0


@dataclass(slots=True)
class ProviderConfig:
    command: str = "/home/matt/.local/bin/uvx"
    args: list[str] = field(default_factory=lambda: ["--from", "tradingview-mcp-server", "tradingview-mcp"])
    timeframe: str = "1D"


@dataclass(slots=True)
class BrokerConfig:
    name: str = "none"
    endpoint_env: str = "alpaca_endpoint"
    key_env: str = "alpaca_key"
    secret_env: str = "alpaca_secret"
    paper_only: bool = True
    submit_orders: bool = False


@dataclass(slots=True)
class BotConfig:
    ledger_path: Path
    starting_cash: float = 100_000.0
    max_position_pct: float = 0.10
    max_open_positions: int = 5
    commission_pct: float = 0.0
    slippage_pct: float = 0.02
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    screener: ScreenerConfig = field(default_factory=ScreenerConfig)
    optimizer: OptimizerConfig = field(default_factory=OptimizerConfig)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    watchlist: list[WatchItem] = field(default_factory=list)


def _str_list(values: object, *, upper: bool = False, lower: bool = False) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    result = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        if upper:
            text = text.upper()
        if lower:
            text = text.lower()
        result.append(text)
    return result


def load_config(path: str | Path) -> BotConfig:
    path = Path(path)
    data = tomllib.loads(path.read_text())

    execution_data = data.get("execution", {})
    execution = ExecutionConfig(
        dry_run=bool(execution_data.get("dry_run", True)),
        live_trading_enabled=bool(execution_data.get("live_trading_enabled", False)),
        scan_interval_seconds=int(execution_data.get("scan_interval_seconds", 900)),
    )
    if not execution.dry_run or execution.live_trading_enabled:
        raise ValueError("live trading is out of scope for this bot; set dry_run=true and live_trading_enabled=false")

    strategy_data = data.get("strategy", {})
    strategy = StrategyConfig(
        entry_score=float(strategy_data.get("entry_score", 65.0)),
        exit_score=float(strategy_data.get("exit_score", 35.0)),
        max_rsi_for_entry=float(strategy_data.get("max_rsi_for_entry", 70.0)),
        stop_loss_pct=float(strategy_data.get("stop_loss_pct", 0.07)),
        take_profit_pct=float(strategy_data.get("take_profit_pct", 0.15)),
    )

    selection_data = data.get("selection", {})
    selection_mode = str(selection_data.get("mode", "ranked")).strip().lower()
    if selection_mode not in {"ranked", "sequential"}:
        raise ValueError("selection.mode must be either 'ranked' or 'sequential'")
    selection = SelectionConfig(
        mode=selection_mode,
        preview_top=max(0, int(selection_data.get("preview_top", 10))),
    )

    screener_data = data.get("screener", {})
    screener_source = str(screener_data.get("source", "curated")).strip().lower()
    if screener_source not in {"curated", "mcp", "hybrid"}:
        raise ValueError("screener.source must be one of 'curated', 'mcp', or 'hybrid'")
    screener = ScreenerConfig(
        enabled=bool(screener_data.get("enabled", False)),
        source=screener_source,
        universes=_str_list(screener_data.get("universes", ["watchlist"]), lower=True) or ["watchlist"],
        exchanges=_str_list(screener_data.get("exchanges", ["NASDAQ", "NYSE"]), upper=True) or ["NASDAQ", "NYSE"],
        dynamic_sources=_str_list(
            screener_data.get(
                "dynamic_sources",
                ["rating_strong_buy", "rating_buy", "volume_breakout", "smart_volume", "top_gainers"],
            ),
            lower=True,
        )
        or ["rating_strong_buy", "rating_buy", "volume_breakout", "smart_volume", "top_gainers"],
        per_source_limit=max(1, int(screener_data.get("per_source_limit", 50))),
        max_candidates=max(1, int(screener_data.get("max_candidates", 100))),
        exclude_symbols=_str_list(screener_data.get("exclude_symbols", []), upper=True),
    )

    optimizer_data = data.get("optimizer", {})
    optimizer = OptimizerConfig(
        enabled=bool(optimizer_data.get("enabled", True)),
        cash_reserve_pct=max(0.0, min(0.95, float(optimizer_data.get("cash_reserve_pct", 0.05)))),
        max_new_buys_per_scan=max(0, int(optimizer_data.get("max_new_buys_per_scan", 3))),
        min_position_notional=max(0.0, float(optimizer_data.get("min_position_notional", 25.0))),
    )

    provider_data = data.get("provider", {})
    provider = ProviderConfig(
        command=str(provider_data.get("command", "/home/matt/.local/bin/uvx")),
        args=[str(arg) for arg in provider_data.get("args", ["--from", "tradingview-mcp-server", "tradingview-mcp"])],
        timeframe=str(provider_data.get("timeframe", "1D")),
    )

    broker_data = data.get("broker", {})
    broker = BrokerConfig(
        name=str(broker_data.get("name", "none")).lower(),
        endpoint_env=str(broker_data.get("endpoint_env", "alpaca_endpoint")),
        key_env=str(broker_data.get("key_env", "alpaca_key")),
        secret_env=str(broker_data.get("secret_env", "alpaca_secret")),
        paper_only=bool(broker_data.get("paper_only", True)),
        submit_orders=bool(broker_data.get("submit_orders", False)),
    )
    if not broker.paper_only:
        raise ValueError("broker config must remain paper_only=true")
    if broker.submit_orders and broker.name != "alpaca":
        raise ValueError("broker order submission currently supports only Alpaca paper accounts")

    watchlist = [
        WatchItem(symbol=str(item["symbol"]).upper(), exchange=str(item.get("exchange", "NASDAQ")).upper())
        for item in data.get("watchlist", [])
    ]
    if not watchlist:
        raise ValueError("config must include at least one [[watchlist]] item")

    return BotConfig(
        ledger_path=Path(data.get("ledger_path", "data/paper-ledger.sqlite3")),
        starting_cash=float(data.get("starting_cash", 100_000.0)),
        max_position_pct=float(data.get("max_position_pct", 0.10)),
        max_open_positions=int(data.get("max_open_positions", 5)),
        commission_pct=float(data.get("commission_pct", 0.0)),
        slippage_pct=float(data.get("slippage_pct", 0.02)),
        execution=execution,
        strategy=strategy,
        selection=selection,
        screener=screener,
        optimizer=optimizer,
        provider=provider,
        broker=broker,
        watchlist=watchlist,
    )
