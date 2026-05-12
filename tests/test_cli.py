import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stonks_bot import cli
from stonks_bot.ledger import PaperLedger
from stonks_bot.options import OptionsPaperLedger


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

def test_alpaca_sync_maps_crypto_broker_symbols_to_screening_symbols(tmp_path, monkeypatch, capsys):
    for key in ["alpaca_endpoint", "alpaca_key", "alpaca_secret", "ALPACA_ENDPOINT", "ALPACA_KEY", "ALPACA_SECRET"]:
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    config_path.write_text(
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
symbol = "BTCUSDT"
exchange = "BINANCE"
asset_class = "crypto"
broker_symbol = "BTC/USD"
"""
    )
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
            return {"status": "ACTIVE", "cash": "9000.00", "equity": "10000.00", "buying_power": "18000.00"}

        def get_positions(self):
            return [
                {
                    "symbol": "BTC/USD",
                    "qty": "0.2",
                    "avg_entry_price": "50000",
                    "current_price": "51000",
                    "asset_id": "asset-btcusd",
                }
            ]

    monkeypatch.setattr(cli, "AlpacaPaperClient", FakeClient)

    result = cli.main(["alpaca-sync", "--config", str(config_path), "--env", str(env_path), "--yes"])

    output = capsys.readouterr().out
    assert result == 0
    assert "synced_positions=1" in output
    ledger = PaperLedger(tmp_path / "ledger.sqlite3", starting_cash=10_000)
    position = ledger.get_position("BTCUSDT")
    assert position is not None
    assert position.exchange == "BINANCE"
    assert position.quantity == 0.2
    assert position.metadata["asset_class"] == "crypto"
    assert position.metadata["broker_symbol"] == "BTC/USD"


def test_options_sync_cash_uses_alpaca_options_buying_power_without_orders(tmp_path, monkeypatch, capsys):
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
            self.submitted_orders = []

        def get_account(self):
            return {
                "status": "ACTIVE",
                "cash": "10000.00",
                "equity": "61200.00",
                "buying_power": "126337.95",
                "options_buying_power": "63168.55",
                "trading_blocked": False,
                "account_blocked": False,
                "pattern_day_trader": False,
            }

    monkeypatch.setattr(cli, "AlpacaPaperClient", FakeClient)

    result = cli.main(["options-sync-cash", "--config", str(config_path), "--env", str(env_path)])

    output = capsys.readouterr().out
    assert result == 0
    assert "READ-ONLY Alpaca paper options buying-power sync" in output
    assert "cash=63168.55" in output
    assert "source=alpaca_options_buying_power" in output
    assert "paper-key" not in output
    assert "paper-secret" not in output

    options_ledger = OptionsPaperLedger(tmp_path / "ledger.sqlite3", starting_cash=10_000)
    assert options_ledger.cash == 63_168.55
    assert options_ledger.cash_source == "alpaca_options_buying_power"


def test_backtest_farm_dry_plan_prints_research_matrix_without_mcp_calls(tmp_path, capsys):
    research_config = tmp_path / "backtest.toml"
    research_config.write_text(
        """
name = "unit-backtest"
symbols = ["SPY"]
strategies = ["rsi", "supertrend"]
periods = ["1y"]
intervals = ["1d"]
max_runs = 2
output_path = "data/research/unit.jsonl"
"""
    )

    result = cli.main(["backtest-farm", "--research-config", str(research_config), "--dry-plan"])

    output = capsys.readouterr().out
    assert result == 0
    assert "RESEARCH ONLY" in output
    assert '"experiment_count": 2' in output
    assert '"strategy": "rsi"' in output
    assert '"strategy": "supertrend"' in output


def test_farm_variant_after_equity_close_does_not_start_mcp_provider(tmp_path, monkeypatch):
    config_path = tmp_path / "variant.toml"
    ledger_path = tmp_path / "ledger.sqlite3"
    config_path.write_text(
        f"""
name = "unit-after-hours"
ledger_path = "{ledger_path}"
starting_cash = 10000

[execution]
dry_run = true
live_trading_enabled = false
scan_interval_seconds = 900
market_hours_only = true
market_timezone = "America/New_York"
market_open = "04:00"
market_close = "20:00"

[screener]
enabled = true
source = "curated"
universes = ["watchlist"]
dynamic_sources = []
max_candidates = 50

[broker]
submit_orders = false

[[watchlist]]
symbol = "AAPL"
exchange = "NASDAQ"
"""
    )

    def fail_if_started(_config):
        raise AssertionError("MCP provider should not start after the equity session closes")

    monkeypatch.setattr(cli, "_tradingview_provider", fail_if_started)

    report = asyncio.run(
        cli._farm_run_variant(
            cli.FarmVariant(name="unit-after-hours", path=config_path),
            now=datetime(2026, 5, 12, 22, 30, tzinfo=ZoneInfo("America/New_York")),
        )
    )

    assert "unit-after-hours" in report
    assert "equity session closed" in report.lower()
    assert "provider scan paused; no MCP calls submitted" in report
    assert "Portfolio:" in report


class _FakeTradingViewContext:
    def __init__(self, provider):
        self.provider = provider
        self.entered = 0

    async def __aenter__(self):
        self.entered += 1
        return self.provider

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _CryptoOnlyProvider:
    def __init__(self):
        self.calls = []
        self.last_discovery_notes = []

    async def combined_analysis(self, symbol, exchange, timeframe):
        self.calls.append((symbol, exchange, timeframe))
        return {
            "technical": {
                "price_data": {"current_price": 50000.0},
                "timeframe_context": {"bias": "Bullish", "bias_reasons": []},
                "rsi": {"value": 55.0},
                "macd": {"crossover": "Bullish"},
                "sma": {"signals": ["Price above SMA50 (bullish)"]},
                "ema": {"signals": ["Price above EMA20 (short-term bullish)"]},
            }
        }



def test_farm_variant_after_equity_close_keeps_crypto_active_without_scoring_stocks(tmp_path, monkeypatch):
    config_path = tmp_path / "variant.toml"
    ledger_path = tmp_path / "ledger.sqlite3"
    config_path.write_text(
        f"""
name = "unit-crypto-after-hours"
ledger_path = "{ledger_path}"
starting_cash = 10000

[execution]
dry_run = true
live_trading_enabled = false
scan_interval_seconds = 900
market_hours_only = true
market_timezone = "America/New_York"
market_open = "04:00"
market_close = "20:00"

[screener]
enabled = true
source = "curated"
universes = ["watchlist"]
dynamic_sources = []
max_candidates = 50

[broker]
submit_orders = false

[[watchlist]]
symbol = "AAPL"
exchange = "NASDAQ"

[[watchlist]]
symbol = "BTCUSDT"
exchange = "BINANCE"
asset_class = "crypto"
broker_symbol = "BTC/USD"
"""
    )

    provider = _CryptoOnlyProvider()
    context = _FakeTradingViewContext(provider)
    monkeypatch.setattr(cli, "_tradingview_provider", lambda _config: context)

    report = asyncio.run(
        cli._farm_run_variant(
            cli.FarmVariant(name="unit-crypto-after-hours", path=config_path),
            now=datetime(2026, 5, 12, 22, 30, tzinfo=ZoneInfo("America/New_York")),
        )
    )

    assert context.entered == 1
    assert provider.calls == [("BTCUSDT", "BINANCE", "4h")]
    assert "equity session closed" in report.lower()
    assert "paused 1 equity candidate" in report
    assert "AAPL" not in report
    assert "PAPER BUY BTCUSDT" in report
