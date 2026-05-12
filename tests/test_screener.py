from stonks_bot.config import BotConfig, ExecutionConfig, ProviderConfig, ScreenerConfig, StrategyConfig, WatchItem
from stonks_bot.screener import build_candidate_universe, build_static_candidate_universe, merge_candidate_universes, normalize_watch_item


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
        screener=ScreenerConfig(enabled=True, universes=["watchlist", "etf_core"], max_candidates=4, exclude_symbols=["SPY"]),
        watchlist=[WatchItem(symbol="AAPL", exchange="NASDAQ"), WatchItem(symbol="QQQ", exchange="NASDAQ")],
    )


def test_build_candidate_universe_expands_curated_universes_dedupes_and_excludes(tmp_path):
    config = _config(tmp_path)

    candidates = build_candidate_universe(config)

    assert [(item.symbol, item.exchange) for item in candidates] == [
        ("AAPL", "NASDAQ"),
        ("QQQ", "NASDAQ"),
        ("IWM", "NYSE"),
        ("DIA", "NYSE"),
    ]


def test_mcp_source_static_universe_uses_watchlist_only(tmp_path):
    config = _config(tmp_path)
    config.screener = ScreenerConfig(
        enabled=True,
        source="mcp",
        universes=["watchlist", "etf_core", "nasdaq_mega"],
        max_candidates=50,
        exclude_symbols=[],
    )

    candidates = build_static_candidate_universe(config)

    assert [(item.symbol, item.exchange) for item in candidates] == [("AAPL", "NASDAQ"), ("QQQ", "NASDAQ")]


def test_indices_universe_adds_core_index_etfs_without_full_market_scan(tmp_path):
    config = _config(tmp_path)
    config.screener = ScreenerConfig(
        enabled=True,
        source="curated",
        universes=["indices", "watchlist"],
        max_candidates=8,
        exclude_symbols=[],
    )

    candidates = build_static_candidate_universe(config)

    assert [(item.symbol, item.exchange) for item in candidates] == [
        ("SPY", "NYSE"),
        ("QQQ", "NASDAQ"),
        ("IWM", "NYSE"),
        ("DIA", "NYSE"),
        ("VTI", "NYSE"),
        ("AAPL", "NASDAQ"),
    ]


def test_merge_candidate_universes_caps_dynamic_candidates_but_protects_open_positions(tmp_path):
    config = _config(tmp_path)
    config.screener.max_candidates = 2
    config.screener.exclude_symbols = ["OLD"]
    open_items = [WatchItem("OLD", "NYSE")]
    static_items = [WatchItem("AAPL", "NASDAQ")]
    dynamic_items = [WatchItem("MSFT", "NASDAQ"), WatchItem("NVDA", "NASDAQ")]

    candidates = merge_candidate_universes(config, open_items, static_items, dynamic_items, protected_symbols={"OLD"})

    assert [(item.symbol, item.exchange) for item in candidates] == [
        ("OLD", "NYSE"),
        ("AAPL", "NASDAQ"),
    ]


def test_normalize_watch_item_parses_tradingview_exchange_prefix_and_skips_preferreds():
    assert normalize_watch_item("NASDAQ:AAPL", "NYSE") == WatchItem("AAPL", "NASDAQ")
    assert normalize_watch_item("NYSE:BRK.B", "NYSE") == WatchItem("BRK.B", "NYSE")
    assert normalize_watch_item("NYSE:ABR/PD", "NYSE") is None


def test_normalize_watch_item_accepts_crypto_pairs_and_infers_asset_class():
    item = normalize_watch_item("BINANCE:BTC/USDT", "NASDAQ")

    assert item is not None
    assert item.symbol == "BTCUSDT"
    assert item.exchange == "BINANCE"
    assert item.asset_class == "crypto"


def test_static_candidate_universe_can_include_curated_crypto_basket(tmp_path):
    config = _config(tmp_path)
    config.screener.universes = ["crypto_major"]
    config.screener.max_candidates = 3

    candidates = build_static_candidate_universe(config)

    assert [(item.symbol, item.exchange, item.asset_class, item.broker_symbol) for item in candidates] == [
        ("BTCUSDT", "BINANCE", "crypto", "BTC/USD"),
        ("ETHUSDT", "BINANCE", "crypto", "ETH/USD"),
        ("SOLUSDT", "BINANCE", "crypto", "SOL/USD"),
    ]
