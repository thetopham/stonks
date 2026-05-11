from __future__ import annotations

from pathlib import Path
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .models import Position, Trade


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PaperLedger:
    def __init__(self, path: str | Path, starting_cash: float = 100_000.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema(starting_cash)

    def _init_schema(self, starting_cash: float) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                exchange TEXT NOT NULL,
                quantity REAL NOT NULL,
                entry_price REAL NOT NULL,
                entry_time TEXT NOT NULL,
                last_price REAL,
                highest_price REAL,
                thesis TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                notional REAL NOT NULL,
                reason TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                cash_after REAL NOT NULL,
                pnl_realized REAL NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        if self.conn.execute("SELECT value FROM state WHERE key='cash'").fetchone() is None:
            self.conn.execute("INSERT INTO state(key, value) VALUES('cash', ?)", (str(float(starting_cash)),))
        self.conn.commit()

    @property
    def cash(self) -> float:
        row = self.conn.execute("SELECT value FROM state WHERE key='cash'").fetchone()
        return float(row["value"])

    def _set_cash(self, value: float) -> None:
        self.conn.execute("UPDATE state SET value=? WHERE key='cash'", (str(float(value)),))

    def get_position(self, symbol: str) -> Position | None:
        row = self.conn.execute("SELECT * FROM positions WHERE symbol=?", (symbol.upper(),)).fetchone()
        if row is None:
            return None
        return Position(
            symbol=row["symbol"],
            exchange=row["exchange"],
            quantity=float(row["quantity"]),
            entry_price=float(row["entry_price"]),
            entry_time=row["entry_time"],
            last_price=float(row["last_price"]) if row["last_price"] is not None else None,
            highest_price=float(row["highest_price"]) if row["highest_price"] is not None else None,
            thesis=row["thesis"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def list_positions(self) -> list[Position]:
        rows = self.conn.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
        return [self.get_position(row["symbol"]) for row in rows if self.get_position(row["symbol"]) is not None]

    def open_position_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()
        return int(row["n"])

    def buy(self, symbol: str, exchange: str, price: float, notional: float, reason: str, metadata: dict[str, Any]) -> Trade:
        symbol = symbol.upper()
        exchange = exchange.upper()
        if self.get_position(symbol) is not None:
            raise ValueError(f"position for {symbol} is already open")
        if price <= 0 or notional <= 0:
            raise ValueError("price and notional must be positive")
        if notional > self.cash + 1e-9:
            raise ValueError("not enough paper cash")

        quantity = notional / price
        cash_after = self.cash - notional
        timestamp = _now()
        self.conn.execute(
            """
            INSERT INTO positions(symbol, exchange, quantity, entry_price, entry_time, last_price, highest_price, thesis, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, exchange, quantity, price, timestamp, price, price, reason, json.dumps(metadata, sort_keys=True)),
        )
        self._set_cash(cash_after)
        trade_id = self._insert_trade(symbol, exchange, "BUY", quantity, price, notional, reason, timestamp, cash_after, 0.0, metadata)
        self.conn.commit()
        return Trade(trade_id, symbol, exchange, "BUY", quantity, price, notional, reason, timestamp, cash_after, 0.0, metadata)

    def sell(self, symbol: str, price: float, reason: str, metadata: dict[str, Any]) -> Trade:
        symbol = symbol.upper()
        position = self.get_position(symbol)
        if position is None:
            raise ValueError(f"no open position for {symbol}")
        if price <= 0:
            raise ValueError("price must be positive")

        proceeds = position.quantity * price
        pnl = (price - position.entry_price) * position.quantity
        cash_after = self.cash + proceeds
        timestamp = _now()
        self.conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self._set_cash(cash_after)
        trade_id = self._insert_trade(symbol, position.exchange, "SELL", position.quantity, price, proceeds, reason, timestamp, cash_after, pnl, metadata)
        self.conn.commit()
        return Trade(trade_id, symbol, position.exchange, "SELL", position.quantity, price, proceeds, reason, timestamp, cash_after, pnl, metadata)

    def mark_price(self, symbol: str, price: float) -> None:
        position = self.get_position(symbol)
        if position is None or price <= 0:
            return
        highest = max(position.highest_price or position.entry_price, price)
        self.conn.execute("UPDATE positions SET last_price=?, highest_price=? WHERE symbol=?", (price, highest, symbol.upper()))
        self.conn.commit()

    def reset_to_broker_snapshot(self, cash: float, positions: list[dict[str, Any]]) -> None:
        timestamp = _now()
        self.conn.execute("DELETE FROM positions")
        self.conn.execute("DELETE FROM trades")
        self._set_cash(float(cash))
        for position in positions:
            symbol = str(position["symbol"]).upper()
            exchange = str(position.get("exchange", "NASDAQ")).upper()
            quantity = float(position["quantity"])
            entry_price = float(position["entry_price"])
            last_price_raw = position.get("last_price", entry_price)
            last_price = float(last_price_raw) if last_price_raw is not None else entry_price
            highest_price = max(entry_price, last_price)
            thesis = str(position.get("thesis") or "Synced from Alpaca account snapshot")
            metadata = dict(position.get("metadata") or {})
            self.conn.execute(
                """
                INSERT INTO positions(symbol, exchange, quantity, entry_price, entry_time, last_price, highest_price, thesis, metadata_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, exchange, quantity, entry_price, timestamp, last_price, highest_price, thesis, json.dumps(metadata, sort_keys=True)),
            )
        self.conn.commit()

    def _insert_trade(self, symbol: str, exchange: str, side: str, quantity: float, price: float, notional: float, reason: str, timestamp: str, cash_after: float, pnl_realized: float, metadata: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO trades(symbol, exchange, side, quantity, price, notional, reason, timestamp, cash_after, pnl_realized, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, exchange, side, quantity, price, notional, reason, timestamp, cash_after, pnl_realized, json.dumps(metadata, sort_keys=True)),
        )
        return int(cur.lastrowid)

    def list_trades(self) -> list[Trade]:
        rows = self.conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
        return [
            Trade(
                id=int(row["id"]),
                symbol=row["symbol"],
                exchange=row["exchange"],
                side=row["side"],
                quantity=float(row["quantity"]),
                price=float(row["price"]),
                notional=float(row["notional"]),
                reason=row["reason"],
                timestamp=row["timestamp"],
                cash_after=float(row["cash_after"]),
                pnl_realized=float(row["pnl_realized"]),
                metadata=json.loads(row["metadata_json"] or "{}"),
            )
            for row in rows
        ]

    def close(self) -> None:
        self.conn.close()
