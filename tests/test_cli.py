from pathlib import Path

from stonks_bot import cli
from stonks_bot.ledger import PaperLedger


def _write_config(path: Path) -> None:
    path.write_text(
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


def test_alpaca_check_prints_read_only_account_summary_without_revealing_credentials(tmp_path, monkeypatch, capsys):
    for key in ["alpaca_endpoint", "alpaca_key", "alpaca_secret", "ALPACA_ENDPOINT", "ALPACA_KEY", "ALPACA_SECRET"]:
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    _write_config(config_path)
    env_path.write_text(
        "\n".join(
            [
                "alpaca_endpoint=https://paper-api.alpaca.markets/v2",
                "alpaca_key=paper-key",
                "alpaca_secret=paper-secret",
            ]
        )
    )

    class FakeClient:
        def __init__(self, credentials):
            assert credentials.key == "paper-key"
            assert credentials.secret == "paper-secret"

        def get_account(self):
            return {
                "status": "ACTIVE",
                "equity": "100000",
                "buying_power": "200000",
                "trading_blocked": False,
                "account_blocked": False,
                "pattern_day_trader": False,
            }

    monkeypatch.setattr(cli, "AlpacaPaperClient", FakeClient)

    result = cli.main(["alpaca-check", "--config", str(config_path), "--env", str(env_path)])

    output = capsys.readouterr().out
    assert result == 0
    assert "Boundary: READ-ONLY Alpaca paper account check" in output
    assert "status=ACTIVE" in output
    assert "paper-key" not in output
    assert "paper-secret" not in output


def test_alpaca_check_reports_missing_secret_without_traceback(tmp_path, monkeypatch, capsys):
    for key in ["alpaca_endpoint", "alpaca_key", "alpaca_secret", "ALPACA_ENDPOINT", "ALPACA_KEY", "ALPACA_SECRET"]:
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    _write_config(config_path)
    env_path.write_text(
        "\n".join(
            [
                "alpaca_endpoint=https://paper-api.alpaca.markets/v2",
                "alpaca_key=paper-key",
                "alpaca_secret=",
            ]
        )
    )

    result = cli.main(["alpaca-check", "--config", str(config_path), "--env", str(env_path)])

    captured = capsys.readouterr()
    assert result == 2
    assert "Alpaca check failed: missing Alpaca credential env value(s): alpaca_secret" in captured.err
    assert "Traceback" not in captured.err
    assert "paper-key" not in captured.err


def test_alpaca_sync_yes_resets_local_ledger_to_sanitized_account_snapshot(tmp_path, monkeypatch, capsys):
    for key in ["alpaca_endpoint", "alpaca_key", "alpaca_secret", "ALPACA_ENDPOINT", "ALPACA_KEY", "ALPACA_SECRET"]:
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    _write_config(config_path)
    env_path.write_text(
        "\n".join(
            [
                "alpaca_endpoint=https://paper-api.alpaca.markets/v2",
                "alpaca_key=paper-key",
                "alpaca_secret=paper-secret",
            ]
        )
    )
    stale_ledger = PaperLedger(tmp_path / "ledger.sqlite3", starting_cash=10_000)
    stale_ledger.buy("OLD", "NASDAQ", price=10.0, notional=100.0, reason="stale", metadata={})
    stale_ledger.close()

    class FakeClient:
        def __init__(self, credentials):
            assert credentials.key == "paper-key"
            assert credentials.secret == "paper-secret"

        def get_account(self):
            return {
                "status": "ACTIVE",
                "cash": "4321.00",
                "equity": "5000.00",
                "buying_power": "8642.00",
                "trading_blocked": False,
                "account_blocked": False,
                "pattern_day_trader": False,
            }

        def get_positions(self):
            return [
                {
                    "symbol": "AAPL",
                    "exchange": "NASDAQ",
                    "qty": "3",
                    "avg_entry_price": "150.25",
                    "current_price": "151.50",
                    "asset_id": "asset-aapl",
                }
            ]

    monkeypatch.setattr(cli, "AlpacaPaperClient", FakeClient)

    result = cli.main(["alpaca-sync", "--config", str(config_path), "--env", str(env_path), "--yes"])

    output = capsys.readouterr().out
    assert result == 0
    assert "Boundary: READ-ONLY Alpaca paper account sync" in output
    assert "synced_positions=1" in output
    assert "paper-key" not in output
    assert "paper-secret" not in output
    assert list(tmp_path.glob("ledger.sqlite3.bak-*"))
    ledger = PaperLedger(tmp_path / "ledger.sqlite3", starting_cash=10_000)
    assert ledger.cash == 4321.00
    assert ledger.get_position("OLD") is None
    position = ledger.get_position("AAPL")
    assert position is not None
    assert position.quantity == 3
    assert position.entry_price == 150.25
    assert position.last_price == 151.50
    assert position.metadata["broker_synced"] is True
