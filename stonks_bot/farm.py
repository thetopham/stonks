from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import tomllib

from .config import BotConfig, load_config
from .ledger import PaperLedger

FARM_BOUNDARY = "STRATEGY FARM SHADOW MODE — local SQLite ledgers only; no broker orders"


@dataclass(slots=True)
class FarmVariant:
    name: str
    path: Path
    description: str = ""
    enabled: bool = True


def _as_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _variant_metadata(path: Path) -> dict[str, Any]:
    data = tomllib.loads(path.read_text())
    return {
        "name": str(data.get("name") or path.stem),
        "description": str(data.get("description") or ""),
        "enabled": _as_bool(data.get("enabled"), True),
    }


def discover_farm_variants(farm_dir: str | Path) -> list[FarmVariant]:
    root = Path(farm_dir)
    if not root.exists():
        return []
    variants: list[FarmVariant] = []
    for path in sorted(root.glob("*.toml")):
        if path.name.startswith("_"):
            continue
        metadata = _variant_metadata(path)
        variants.append(
            FarmVariant(
                name=metadata["name"],
                path=path,
                description=metadata["description"],
                enabled=bool(metadata["enabled"]),
            )
        )
    return variants


def assert_shadow_safe(config: BotConfig, *, variant_name: str | None = None) -> None:
    label = f"{variant_name}: " if variant_name else ""
    if not config.execution.dry_run or config.execution.live_trading_enabled:
        raise ValueError(f"{label}farm variants must keep dry_run=true and live_trading_enabled=false")
    if config.broker.submit_orders:
        raise ValueError(f"{label}farm variants must keep broker.submit_orders=false")
    if config.options.auto_trade or config.options.submit_orders:
        raise ValueError(f"{label}farm variants must keep options.auto_trade=false and options.submit_orders=false")


def _empty_metrics(config: BotConfig) -> dict[str, Any]:
    return {
        "cash": round(config.starting_cash, 2),
        "position_value": 0.0,
        "equity": round(config.starting_cash, 2),
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_return_pct": 0.0,
        "open_positions": 0,
        "trades_recorded": 0,
        "latest_trade_at": None,
    }


def _ledger_metrics(config: BotConfig, ledger: PaperLedger) -> dict[str, Any]:
    positions = ledger.list_positions()
    trades = ledger.list_trades()
    realized_pnl = sum(trade.pnl_realized for trade in trades if trade.side == "SELL")
    position_value = sum((position.last_price or position.entry_price) * position.quantity for position in positions)
    cost_basis = sum(position.entry_price * position.quantity for position in positions)
    unrealized_pnl = position_value - cost_basis
    equity = ledger.cash + position_value
    total_return_pct = ((equity / config.starting_cash) - 1) * 100 if config.starting_cash else 0.0
    latest_trade_at = trades[-1].timestamp if trades else None
    return {
        "cash": round(ledger.cash, 2),
        "position_value": round(position_value, 2),
        "equity": round(equity, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_return_pct": round(total_return_pct, 2),
        "open_positions": len(positions),
        "trades_recorded": len(trades),
        "latest_trade_at": latest_trade_at,
    }


def summarize_variant(variant: FarmVariant) -> dict[str, Any]:
    try:
        config = load_config(variant.path)
        assert_shadow_safe(config, variant_name=variant.name)
        ledger_exists = config.ledger_path.exists()
        metrics = _empty_metrics(config)
        if ledger_exists:
            ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
            try:
                metrics = _ledger_metrics(config, ledger)
            finally:
                ledger.close()
        status = {
            "name": variant.name,
            "description": variant.description,
            "enabled": variant.enabled,
            "safe_shadow": True,
            "error": None,
            "config_path": str(variant.path),
            "ledger_path": str(config.ledger_path),
            "ledger_exists": ledger_exists,
            "timeframe": config.provider.timeframe,
            "scan_interval_seconds": config.execution.scan_interval_seconds,
            "cadence_minutes": round(config.execution.scan_interval_seconds / 60, 2),
            "initial_delay_seconds": config.execution.initial_delay_seconds,
            "entry_score": config.strategy.entry_score,
            "exit_score": config.strategy.exit_score,
            "max_rsi_for_entry": config.strategy.max_rsi_for_entry,
            "stop_loss_pct": config.strategy.stop_loss_pct,
            "take_profit_pct": config.strategy.take_profit_pct,
            "max_candidates": config.screener.max_candidates,
            "per_source_limit": config.screener.per_source_limit,
            "dynamic_sources": config.screener.dynamic_sources,
            "provider_cache_enabled": config.provider.cache_enabled,
            "provider_cache_path": str(config.provider.cache_path),
            "broker_submit_orders": config.broker.submit_orders,
            "options_auto_trade": config.options.auto_trade,
            "options_submit_orders": config.options.submit_orders,
        }
        status.update(metrics)
        return status
    except Exception as exc:
        return {
            "name": variant.name,
            "description": variant.description,
            "enabled": variant.enabled,
            "safe_shadow": False,
            "error": f"{type(exc).__name__}: {exc}",
            "config_path": str(variant.path),
        }


def collect_farm_status(farm_dir: str | Path, *, include_disabled: bool = False) -> dict[str, Any]:
    variants = discover_farm_variants(farm_dir)
    summaries = [summarize_variant(variant) for variant in variants if include_disabled or variant.enabled]
    sortable = [summary for summary in summaries if summary.get("safe_shadow")]
    leaderboard = sorted(
        sortable,
        key=lambda row: (
            -float(row.get("total_return_pct", 0.0)),
            -float(row.get("equity", 0.0)),
            str(row.get("name", "")),
        ),
    )
    return {
        "boundary": FARM_BOUNDARY,
        "farm_dir": str(farm_dir),
        "variant_count": len(summaries),
        "enabled_count": sum(1 for variant in variants if variant.enabled),
        "variants": summaries,
        "leaderboard": leaderboard,
    }


def format_farm_status(status: dict[str, Any]) -> str:
    lines = [status["boundary"], f"Farm: {status['farm_dir']} ({status['variant_count']} variants)"]
    variants = status.get("leaderboard") or status.get("variants", [])
    if not variants:
        lines.append("No enabled strategy variants found.")
        return "\n".join(lines)
    lines.append("Leaderboard by total return:")
    lines.append("name                 tf   cadence  return    equity       open trades safe")
    for row in variants:
        if not row.get("safe_shadow"):
            lines.append(f"{str(row.get('name', 'unknown'))[:20]:20} ERROR {row.get('error')}")
            continue
        lines.append(
            f"{str(row['name'])[:20]:20} "
            f"{str(row['timeframe'])[:4]:4} "
            f"{row['cadence_minutes']:>5.0f}m   "
            f"{row['total_return_pct']:>+7.2f}% "
            f"${row['equity']:>10,.2f} "
            f"{row['open_positions']:>4} "
            f"{row['trades_recorded']:>6} yes"
        )
    return "\n".join(lines)
