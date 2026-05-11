from stonks_bot.config import BotConfig, ExecutionConfig, ProviderConfig, StrategyConfig, WatchItem
from stonks_bot.dashboard import collect_dashboard_data, render_dashboard_html
from stonks_bot.ledger import PaperLedger


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
        watchlist=[WatchItem(symbol="AAPL", exchange="NASDAQ"), WatchItem(symbol="MSFT", exchange="NASDAQ")],
    )


def test_collect_dashboard_data_summarizes_paper_portfolio_without_network(tmp_path):
    config = _config(tmp_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    ledger.buy("AAPL", "NASDAQ", price=100, notional=1_000, reason="test entry", metadata={"score": 72})
    ledger.mark_price("AAPL", 110)

    data = collect_dashboard_data(config, ledger, service_names=[])

    assert data["boundary"] == "PAPER ONLY — no broker orders, no live execution"
    assert data["portfolio"]["cash"] == 9_000
    assert data["portfolio"]["position_value"] == 1_100
    assert data["portfolio"]["equity"] == 10_100
    assert data["portfolio"]["unrealized_pnl"] == 100
    assert data["portfolio"]["realized_pnl"] == 0
    assert data["portfolio"]["open_positions"] == 1
    assert data["positions"][0]["symbol"] == "AAPL"
    assert data["positions"][0]["pnl_pct"] == 10
    assert data["positions"][0]["score"] == 72
    assert data["watchlist"] == [
        {"symbol": "AAPL", "exchange": "NASDAQ"},
        {"symbol": "MSFT", "exchange": "NASDAQ"},
    ]


def test_render_dashboard_html_contains_cards_and_paper_boundary(tmp_path):
    config = _config(tmp_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    data = collect_dashboard_data(config, ledger, service_names=[])

    html = render_dashboard_html(data, api_path="/api/dashboard")

    assert "PAPER ONLY" in html
    assert "Portfolio" in html
    assert "Open Positions" in html
    assert "/api/dashboard" in html
