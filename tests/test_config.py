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


def test_load_config_reads_alpaca_broker_without_enabling_order_submission(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
ledger_path = "ledger.sqlite3"

[execution]
dry_run = true
live_trading_enabled = false

[broker]
name = "alpaca"
endpoint_env = "alpaca_endpoint"
key_env = "alpaca_key"
secret_env = "alpaca_secret"
paper_only = true
submit_orders = false

[[watchlist]]
symbol = "AAPL"
exchange = "NASDAQ"
"""
    )

    config = load_config(cfg_path)

    assert config.broker.name == "alpaca"
    assert config.broker.endpoint_env == "alpaca_endpoint"
    assert config.broker.key_env == "alpaca_key"
    assert config.broker.secret_env == "alpaca_secret"
    assert config.broker.paper_only is True
    assert config.broker.submit_orders is False


def test_broker_order_submission_is_allowed_for_paper_alpaca_accounts(tmp_path):
    cfg_path = tmp_path / "paper-broker.toml"
    cfg_path.write_text(
        """
ledger_path = "ledger.sqlite3"

[execution]
dry_run = true
live_trading_enabled = false

[broker]
name = "alpaca"
paper_only = true
submit_orders = true

[[watchlist]]
symbol = "AAPL"
exchange = "NASDAQ"
"""
    )

    config = load_config(cfg_path)

    assert config.broker.name == "alpaca"
    assert config.broker.paper_only is True
    assert config.broker.submit_orders is True


def test_broker_order_submission_still_rejects_non_paper_accounts(tmp_path):
    cfg_path = tmp_path / "bad-broker.toml"
    cfg_path.write_text(
        """
ledger_path = "ledger.sqlite3"

[execution]
dry_run = true
live_trading_enabled = false

[broker]
name = "alpaca"
paper_only = false
submit_orders = true

[[watchlist]]
symbol = "AAPL"
exchange = "NASDAQ"
"""
    )

    try:
        load_config(cfg_path)
    except ValueError as exc:
        assert "paper_only=true" in str(exc)
    else:
        raise AssertionError("expected non-paper broker order submission to be rejected")
