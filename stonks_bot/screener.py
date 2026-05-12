from __future__ import annotations

import re

from .config import BotConfig
from .models import WatchItem, default_crypto_broker_symbol, infer_asset_class


_TRADEABLE_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_CRYPTO_SYMBOL_RE = re.compile(r"^[A-Z0-9]{4,20}$")


CURATED_UNIVERSES: dict[str, list[WatchItem]] = {
    "etf_core": [
        WatchItem("SPY", "NYSE"),
        WatchItem("QQQ", "NASDAQ"),
        WatchItem("IWM", "NYSE"),
        WatchItem("DIA", "NYSE"),
        WatchItem("VTI", "NYSE"),
        WatchItem("XLK", "NYSE"),
        WatchItem("SMH", "NASDAQ"),
        WatchItem("SOXX", "NASDAQ"),
        WatchItem("XLE", "NYSE"),
        WatchItem("XLF", "NYSE"),
        WatchItem("GLD", "NYSE"),
        WatchItem("TLT", "NASDAQ"),
    ],
    "nasdaq_mega": [
        WatchItem("AAPL", "NASDAQ"),
        WatchItem("MSFT", "NASDAQ"),
        WatchItem("NVDA", "NASDAQ"),
        WatchItem("AMZN", "NASDAQ"),
        WatchItem("GOOGL", "NASDAQ"),
        WatchItem("META", "NASDAQ"),
        WatchItem("TSLA", "NASDAQ"),
        WatchItem("AVGO", "NASDAQ"),
        WatchItem("COST", "NASDAQ"),
        WatchItem("NFLX", "NASDAQ"),
        WatchItem("AMD", "NASDAQ"),
        WatchItem("QCOM", "NASDAQ"),
        WatchItem("CSCO", "NASDAQ"),
        WatchItem("AMAT", "NASDAQ"),
        WatchItem("MU", "NASDAQ"),
    ],
    "nyse_mega": [
        WatchItem("JPM", "NYSE"),
        WatchItem("V", "NYSE"),
        WatchItem("MA", "NYSE"),
        WatchItem("WMT", "NYSE"),
        WatchItem("LLY", "NYSE"),
        WatchItem("UNH", "NYSE"),
        WatchItem("XOM", "NYSE"),
        WatchItem("ORCL", "NYSE"),
        WatchItem("HD", "NYSE"),
        WatchItem("PG", "NYSE"),
        WatchItem("JNJ", "NYSE"),
        WatchItem("BAC", "NYSE"),
        WatchItem("KO", "NYSE"),
        WatchItem("CVX", "NYSE"),
        WatchItem("GE", "NYSE"),
        WatchItem("CRM", "NYSE"),
    ],
    "ai_infra": [
        WatchItem("NVDA", "NASDAQ"),
        WatchItem("AMD", "NASDAQ"),
        WatchItem("AVGO", "NASDAQ"),
        WatchItem("ARM", "NASDAQ"),
        WatchItem("MU", "NASDAQ"),
        WatchItem("VRT", "NYSE"),
        WatchItem("ETN", "NYSE"),
        WatchItem("GEV", "NYSE"),
        WatchItem("CEG", "NASDAQ"),
        WatchItem("ANET", "NYSE"),
        WatchItem("MRVL", "NASDAQ"),
        WatchItem("SMR", "NYSE"),
        WatchItem("OKLO", "NYSE"),
        WatchItem("RKLB", "NASDAQ"),
        WatchItem("ASTS", "NASDAQ"),
    ],
    "crypto_major": [
        WatchItem("BTCUSDT", "BINANCE", "crypto", "BTC/USD"),
        WatchItem("ETHUSDT", "BINANCE", "crypto", "ETH/USD"),
        WatchItem("SOLUSDT", "BINANCE", "crypto", "SOL/USD"),
        WatchItem("DOGEUSDT", "BINANCE", "crypto", "DOGE/USD"),
        WatchItem("AVAXUSDT", "BINANCE", "crypto", "AVAX/USD"),
        WatchItem("LINKUSDT", "BINANCE", "crypto", "LINK/USD"),
        WatchItem("LTCUSDT", "BINANCE", "crypto", "LTC/USD"),
        WatchItem("BCHUSDT", "BINANCE", "crypto", "BCH/USD"),
    ],
}


def normalize_watch_item(symbol: str, exchange: str) -> WatchItem | None:
    """Normalize TradingView/MCP symbols to broker-style symbol + exchange pairs."""
    raw_symbol = str(symbol or "").strip().upper()
    raw_exchange = str(exchange or "NASDAQ").strip().upper()
    if not raw_symbol:
        return None

    if ":" in raw_symbol:
        maybe_exchange, maybe_symbol = raw_symbol.split(":", 1)
        if maybe_exchange.strip():
            raw_exchange = maybe_exchange.strip().upper()
        raw_symbol = maybe_symbol.strip().upper()

    asset_class = infer_asset_class(raw_exchange)
    if asset_class == "crypto":
        broker_symbol = default_crypto_broker_symbol(raw_symbol)
        raw_symbol = raw_symbol.replace("/", "").replace("-", "")
        if not _CRYPTO_SYMBOL_RE.match(raw_symbol):
            return None
        return WatchItem(symbol=raw_symbol, exchange=raw_exchange, asset_class="crypto", broker_symbol=broker_symbol)

    # Avoid preferred-share/unit/warrant strings such as BRK/PB or AACBU-like
    # exchange artifacts when possible. The bot can still include them manually
    # via watchlist if the operator really wants them.
    if "/" in raw_symbol or raw_symbol.startswith("^"):
        return None
    if not _TRADEABLE_SYMBOL_RE.match(raw_symbol):
        return None
    return WatchItem(symbol=raw_symbol, exchange=raw_exchange)


def _append_unique(
    items: list[WatchItem],
    seen: set[str],
    symbol: str,
    exchange: str,
    excluded: set[str],
    protected_symbols: set[str] | None = None,
    asset_class: str | None = None,
    broker_symbol: str | None = None,
) -> None:
    if asset_class or broker_symbol:
        item = WatchItem(symbol=symbol, exchange=exchange, asset_class=asset_class or "auto", broker_symbol=broker_symbol)
    else:
        item = normalize_watch_item(symbol, exchange)
    if item is None:
        return
    protected_symbols = protected_symbols or set()
    if item.symbol in seen:
        return
    if item.symbol in excluded and item.symbol not in protected_symbols:
        return
    seen.add(item.symbol)
    items.append(item)


def build_static_candidate_universe(config: BotConfig, *, cap: bool = True) -> list[WatchItem]:
    """Return local/watchlist candidates before dynamic MCP discovery.

    In `source="mcp"` mode, the static seed is intentionally only the manual
    watchlist. Curated baskets are fallback/hybrid helpers, not the main way to
    represent the whole market.
    """
    excluded = {symbol.upper() for symbol in config.screener.exclude_symbols}
    items: list[WatchItem] = []
    seen: set[str] = set()

    if not config.screener.enabled or config.screener.source == "mcp":
        universes = ["watchlist"]
    else:
        universes = config.screener.universes

    for universe in universes:
        if universe == "watchlist":
            for item in config.watchlist:
                _append_unique(items, seen, item.symbol, item.exchange, excluded, asset_class=item.asset_class, broker_symbol=item.broker_symbol)
        else:
            try:
                universe_items = CURATED_UNIVERSES[universe]
            except KeyError as exc:
                available = ", ".join(["watchlist", *sorted(CURATED_UNIVERSES)])
                raise ValueError(f"unknown screener universe '{universe}'; available: {available}") from exc
            for item in universe_items:
                _append_unique(items, seen, item.symbol, item.exchange, excluded, asset_class=item.asset_class, broker_symbol=item.broker_symbol)

        if cap and len(items) >= config.screener.max_candidates:
            return items[: config.screener.max_candidates]

    return items[: config.screener.max_candidates] if cap else items


def merge_candidate_universes(
    config: BotConfig,
    *candidate_lists: list[WatchItem],
    cap: bool = True,
    protected_symbols: set[str] | None = None,
) -> list[WatchItem]:
    """Dedupe candidate lists, apply excludes, and cap work.

    Protected symbols bypass excludes and the max-candidate cap. Runner uses this
    for open positions so exits are always evaluated even if the broad screener
    does not rediscover the symbol.
    """
    excluded = {symbol.upper() for symbol in config.screener.exclude_symbols}
    protected_symbols = {symbol.upper() for symbol in (protected_symbols or set())}
    items: list[WatchItem] = []
    seen: set[str] = set()
    limit = config.screener.max_candidates if cap else None

    for candidate_list in candidate_lists:
        for item in candidate_list:
            if item.asset_class or item.broker_symbol:
                normalized = WatchItem(item.symbol, item.exchange, item.asset_class, item.broker_symbol)
            else:
                normalized = normalize_watch_item(item.symbol, item.exchange)
            if normalized is None or normalized.symbol in seen:
                continue
            if normalized.symbol in excluded and normalized.symbol not in protected_symbols:
                continue
            if limit is not None and len(items) >= limit and normalized.symbol not in protected_symbols:
                continue
            seen.add(normalized.symbol)
            items.append(normalized)
    return items


# Backward-compatible name used by older tests/docs.
def build_candidate_universe(config: BotConfig) -> list[WatchItem]:
    return build_static_candidate_universe(config)
