from pathlib import Path

from stonks_bot.config import load_config


def test_load_config_keeps_live_trading_disabled_by_default(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
ledger_path = "ledger.sqlite3"
starting_cash = 5000
max_position_pct = 0.2
max_open_positions = 2
commission_pct = 0
slippage_pct = 0

[execution]
dry_run = true
scan_interval_seconds = 60

[strategy]
entry_score = 65
exit_score = 35
max_rsi_for_entry = 70
stop_loss_pct = 0.07
take_profit_pct = 0.15

[provider]
command = "/bin/echo"
args = ["fake"]
timeframe = "1D"

[[watchlist]]
symbol = "AAPL"
exchange = "NASDAQ"
"""
    )

    config = load_config(cfg_path)

    assert config.execution.dry_run is True
    assert config.execution.live_trading_enabled is False
    assert config.ledger_path == Path("ledger.sqlite3")
    assert config.watchlist[0].symbol == "AAPL"


def test_live_trading_enabled_is_rejected(tmp_path):
    cfg_path = tmp_path / "bad.toml"
    cfg_path.write_text(
        """
ledger_path = "ledger.sqlite3"

[execution]
dry_run = false
live_trading_enabled = true

[[watchlist]]
symbol = "AAPL"
exchange = "NASDAQ"
"""
    )

    try:
        load_config(cfg_path)
    except ValueError as exc:
        assert "live trading is out of scope" in str(exc)
    else:
        raise AssertionError("expected live trading config to be rejected")
