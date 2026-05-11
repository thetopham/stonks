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
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    watchlist: list[WatchItem] = field(default_factory=list)


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
        provider=provider,
        broker=broker,
        watchlist=watchlist,
    )
