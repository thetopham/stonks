from stonks_bot.config import BotConfig, ExecutionConfig, OptionsConfig, ProviderConfig, StrategyConfig, WatchItem
from stonks_bot.dashboard import collect_dashboard_data, render_dashboard_html
from stonks_bot.ledger import PaperLedger
from stonks_bot.options import OptionCandidate, OptionsPaperLedger


def _config(tmp_path):
    return BotConfig(
        ledger_path=tmp_path / "paper.sqlite3",
        starting_cash=10_000,
        max_position_pct=0.10,
        max_open_positions=3,
        commission_pct=0,
        slippage_pct=0.02,
        execution=ExecutionConfig(dry_run=True, live_trading_enabled=False, scan_interval_seconds=60),
        strategy=StrategyConfig(entry_score=65, exit_score=35, max_rsi_for_entry=70, stop_loss_pct=0.07, take_profit_pct=0.15),
        provider=ProviderConfig(command="fake", args=[], timeframe="1D"),
        options=OptionsConfig(enabled=True, underlying_symbols=["AAPL", "MSFT"], auto_trade=True, submit_orders=False),
        watchlist=[WatchItem(symbol="AAPL", exchange="NASDAQ"), WatchItem(symbol="MSFT", exchange="NASDAQ")],
    )


def test_collect_dashboard_data_summarizes_paper_portfolio_without_network(tmp_path):
    config = _config(tmp_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    ledger.buy("AAPL", "NASDAQ", price=100, notional=1_000, reason="test entry", metadata={"score": 72})
    ledger.mark_price("AAPL", 110)

    data = collect_dashboard_data(config, ledger, service_names=[])

    assert data["boundary"] == "LOCAL PAPER ONLY — no broker orders, no live execution"
    assert data["portfolio"]["cash"] == 9_000
    assert data["portfolio"]["position_value"] == 1_100
    assert data["portfolio"]["equity"] == 10_100
    assert data["portfolio"]["unrealized_pnl"] == 100
    assert data["portfolio"]["realized_pnl"] == 0
    assert data["portfolio"]["open_positions"] == 1
    assert data["positions"][0]["symbol"] == "AAPL"
    assert data["positions"][0]["pnl_pct"] == 10
    assert data["positions"][0]["score"] == 72
    assert data["options"]["enabled"] is True
    assert data["options"]["auto_trade"] is True
    assert data["options"]["submit_orders"] is False
    assert data["options"]["open_positions"] == 0
    assert data["options"]["rules"]["min_dte"] == 30
    assert data["watchlist"] == [
        {"symbol": "AAPL", "exchange": "NASDAQ"},
        {"symbol": "MSFT", "exchange": "NASDAQ"},
    ]


def test_collect_dashboard_data_shows_options_cash_source(tmp_path):
    config = _config(tmp_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    options_ledger.sync_cash(63_168.55, source="alpaca_options_buying_power")

    data = collect_dashboard_data(config, ledger, service_names=[], options_ledger=options_ledger)
    html = render_dashboard_html(data, api_path="/api/dashboard")

    assert data["options"]["cash"] == 63_168.55
    assert data["options"]["cash_source"] == "alpaca_options_buying_power"
    assert "Alpaca options buying power" in html


def test_collect_dashboard_data_includes_options_overlay_positions_and_trades(tmp_path):
    config = _config(tmp_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    candidate = OptionCandidate(
        underlying_symbol="AAPL",
        contract_symbol="AAPL260620C00105000",
        strategy="long call",
        contract_type="call",
        expiration_date="2026-06-20",
        dte=40,
        strike_price=105.0,
        bid=0.95,
        ask=1.00,
        mid=0.975,
        spread_pct=0.05,
        open_interest=500,
        contracts=1,
        max_loss=100.0,
        signal_score=82.0,
        reasons=["test setup"],
    )
    options_ledger.buy_to_open(candidate, reason="test option entry", metadata={"score": 82, "broker_order_id": "paper-option-1"})
    options_ledger.mark_mid("AAPL260620C00105000", 1.20)

    data = collect_dashboard_data(config, ledger, service_names=[], options_ledger=options_ledger)

    assert data["options"]["cash"] == 9900
    assert data["options"]["position_value"] == 120
    assert data["options"]["equity"] == 10020
    assert data["options"]["unrealized_pnl"] == 20
    assert data["options"]["open_positions"] == 1
    assert data["options"]["positions"][0]["contract_symbol"] == "AAPL260620C00105000"
    assert data["options"]["positions"][0]["broker_order_id"] == "paper-option-1"
    assert data["options"]["recent_trades"][0]["side"] == "BUY_TO_OPEN"


def test_render_dashboard_html_contains_cards_and_paper_boundary(tmp_path):
    config = _config(tmp_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    data = collect_dashboard_data(config, ledger, service_names=[])

    html = render_dashboard_html(data, api_path="/api/dashboard")

    assert "PAPER ONLY" in html or "LOCAL PAPER ONLY" in html
    assert "Portfolio" in html
    assert "Open Positions" in html
    assert "Options Overlay" in html
    assert "local paper ledger" in html
    assert "/api/dashboard" in html
