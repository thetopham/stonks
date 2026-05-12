from pathlib import Path

from stonks_bot.config import load_config
from stonks_bot.farm import collect_farm_status, discover_farm_variants, format_farm_status
from stonks_bot.ledger import PaperLedger


def _write_base(path: Path) -> None:
    path.write_text(
        """
ledger_path = "base.sqlite3"
starting_cash = 10000
max_position_pct = 0.10
max_open_positions = 5
commission_pct = 0
slippage_pct = 0.02

[execution]
dry_run = true
live_trading_enabled = false
scan_interval_seconds = 1800

[strategy]
entry_score = 65
exit_score = 35
max_rsi_for_entry = 70
stop_loss_pct = 0.07
take_profit_pct = 0.15

[screener]
enabled = true
source = "mcp"
per_source_limit = 50
max_candidates = 100

[provider]
command = "/bin/echo"
args = ["fake"]
timeframe = "4h"

[broker]
name = "alpaca"
paper_only = true
submit_orders = true

[options]
enabled = true
auto_trade = true
submit_orders = true

[[watchlist]]
symbol = "AAPL"
exchange = "NASDAQ"
"""
    )


def test_config_extends_parent_and_overrides_shadow_farm_knobs(tmp_path):
    base = tmp_path / "base.toml"
    farm_dir = tmp_path / "farm"
    farm_dir.mkdir()
    variant = farm_dir / "original-15m-1d.toml"
    _write_base(base)
    variant.write_text(
        """
extends = "../base.toml"
name = "original-15m-1d"
description = "test variant"
ledger_path = "variant.sqlite3"

[execution]
scan_interval_seconds = 900
initial_delay_seconds = 120

[provider]
timeframe = "1D"

[screener]
per_source_limit = 25
max_candidates = 40

[broker]
submit_orders = false

[options]
enabled = false
auto_trade = false
submit_orders = false
"""
    )

    config = load_config(variant)

    assert config.ledger_path == Path("variant.sqlite3")
    assert config.execution.scan_interval_seconds == 900
    assert config.execution.initial_delay_seconds == 120
    assert config.provider.timeframe == "1d"
    assert config.screener.max_candidates == 40
    assert config.watchlist[0].symbol == "AAPL"
    assert config.broker.submit_orders is False
    assert config.options.auto_trade is False
    assert config.options.submit_orders is False


def test_farm_status_summarizes_local_ledgers_without_network(tmp_path):
    base = tmp_path / "base.toml"
    farm_dir = tmp_path / "farm"
    farm_dir.mkdir()
    variant = farm_dir / "balanced.toml"
    ledger_path = tmp_path / "balanced.sqlite3"
    _write_base(base)
    variant.write_text(
        f"""
extends = "../base.toml"
name = "balanced"
description = "shadow test"
ledger_path = "{ledger_path}"

[execution]
scan_interval_seconds = 1800
initial_delay_seconds = 60

[provider]
timeframe = "4h"

[broker]
submit_orders = false

[options]
enabled = false
auto_trade = false
submit_orders = false
"""
    )
    ledger = PaperLedger(ledger_path, starting_cash=10_000)
    ledger.buy("AAPL", "NASDAQ", price=100, notional=1_000, reason="test", metadata={"score": 70})
    ledger.mark_price("AAPL", 110)
    ledger.close()

    variants = discover_farm_variants(farm_dir)
    status = collect_farm_status(farm_dir)
    text = format_farm_status(status)

    assert [variant.name for variant in variants] == ["balanced"]
    assert status["variant_count"] == 1
    row = status["leaderboard"][0]
    assert row["name"] == "balanced"
    assert row["safe_shadow"] is True
    assert row["timeframe"] == "4h"
    assert row["cadence_minutes"] == 30
    assert row["open_positions"] == 1
    assert row["trades_recorded"] == 1
    assert row["total_return_pct"] == 1.0
    assert "balanced" in text
    assert "STRATEGY FARM SHADOW MODE" in text


def test_farm_status_flags_unsafe_broker_variants(tmp_path):
    base = tmp_path / "base.toml"
    farm_dir = tmp_path / "farm"
    farm_dir.mkdir()
    variant = farm_dir / "unsafe.toml"
    _write_base(base)
    variant.write_text(
        """
extends = "../base.toml"
name = "unsafe"
ledger_path = "unsafe.sqlite3"

[options]
enabled = false
auto_trade = false
submit_orders = false
"""
    )

    status = collect_farm_status(farm_dir)

    assert status["variants"][0]["safe_shadow"] is False
    assert "broker.submit_orders=false" in status["variants"][0]["error"]
