from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CRYPTO_EXCHANGES = {"ALPACA", "BINANCE", "BYBIT", "COINBASE", "KRAKEN", "KUCOIN", "MEXC", "OKX"}

DEFAULT_CRYPTO_BROKER_SYMBOLS = {
    "BTCUSD": "BTC/USD",
    "BTCUSDT": "BTC/USD",
    "ETHUSD": "ETH/USD",
    "ETHUSDT": "ETH/USD",
    "SOLUSD": "SOL/USD",
    "SOLUSDT": "SOL/USD",
    "DOGEUSD": "DOGE/USD",
    "DOGEUSDT": "DOGE/USD",
    "AVAXUSD": "AVAX/USD",
    "AVAXUSDT": "AVAX/USD",
    "LINKUSD": "LINK/USD",
    "LINKUSDT": "LINK/USD",
    "LTCUSD": "LTC/USD",
    "LTCUSDT": "LTC/USD",
    "BCHUSD": "BCH/USD",
    "BCHUSDT": "BCH/USD",
    "UNIUSD": "UNI/USD",
    "UNIUSDT": "UNI/USD",
    "AAVEUSD": "AAVE/USD",
    "AAVEUSDT": "AAVE/USD",
}


def infer_asset_class(exchange: str, asset_class: str | None = None) -> str:
    raw_asset_class = str(asset_class or "").strip().lower()
    if raw_asset_class in {"stock", "stocks", "equity", "equities", "etf", "etfs"}:
        return "equity"
    if raw_asset_class in {"crypto", "cryptocurrency", "coin", "coins"}:
        return "crypto"
    return "crypto" if str(exchange or "").strip().upper() in CRYPTO_EXCHANGES else "equity"


def normalize_crypto_broker_symbol(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    text = str(symbol).strip().upper()
    if not text:
        return None
    return text


def default_crypto_broker_symbol(symbol: str) -> str | None:
    clean = str(symbol or "").strip().upper().replace("/", "").replace("-", "")
    return DEFAULT_CRYPTO_BROKER_SYMBOLS.get(clean)


@dataclass(slots=True)
class Position:
    symbol: str
    exchange: str
    quantity: float
    entry_price: float
    entry_time: str
    last_price: float | None = None
    highest_price: float | None = None
    thesis: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Trade:
    id: int | None
    symbol: str
    exchange: str
    side: str
    quantity: float
    price: float
    notional: float
    reason: str
    timestamp: str
    cash_after: float
    pnl_realized: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WatchItem:
    symbol: str
    exchange: str = "NASDAQ"
    asset_class: str = "auto"
    broker_symbol: str | None = None

    def __post_init__(self) -> None:
        symbol = str(self.symbol or "").strip().upper()
        exchange = str(self.exchange or "NASDAQ").strip().upper()
        if ":" in symbol:
            maybe_exchange, maybe_symbol = symbol.split(":", 1)
            if maybe_exchange.strip():
                exchange = maybe_exchange.strip().upper()
            symbol = maybe_symbol.strip().upper()
        asset_class = infer_asset_class(exchange, self.asset_class)
        broker_symbol = normalize_crypto_broker_symbol(self.broker_symbol)

        if asset_class == "crypto":
            symbol = symbol.replace("/", "").replace("-", "")
            broker_symbol = broker_symbol or default_crypto_broker_symbol(symbol)
        elif not broker_symbol:
            broker_symbol = None

        self.symbol = symbol
        self.exchange = exchange
        self.asset_class = asset_class
        self.broker_symbol = broker_symbol
