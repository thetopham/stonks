from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
