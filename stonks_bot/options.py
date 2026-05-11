from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
import json
import sqlite3

from .config import BotConfig, OptionsConfig
from .ledger import PaperLedger
from .models import WatchItem
from .strategy import generate_signal, score_analysis


@dataclass(slots=True)
class OptionContract:
    symbol: str
    underlying_symbol: str
    type: str
    expiration_date: str
    strike_price: float
    open_interest: float = 0.0
    tradable: bool = True


@dataclass(slots=True)
class OptionQuote:
    symbol: str
    bid: float
    ask: float
    timestamp: str | None = None


@dataclass(slots=True)
class OptionCandidate:
    underlying_symbol: str
    contract_symbol: str
    strategy: str
    contract_type: str
    expiration_date: str
    dte: int
    strike_price: float
    bid: float
    ask: float
    mid: float
    spread_pct: float
    open_interest: float
    contracts: int
    max_loss: float
    signal_score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class OptionPosition:
    contract_symbol: str
    underlying_symbol: str
    strategy: str
    contract_type: str
    strike_price: float
    expiration_date: str
    contracts: int
    entry_price: float
    entry_debit: float
    entry_time: str
    last_mid: float | None = None
    highest_mid: float | None = None
    thesis: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OptionTrade:
    id: int | None
    contract_symbol: str
    underlying_symbol: str
    side: str
    contracts: int
    price: float
    notional: float
    reason: str
    timestamp: str
    cash_after: float
    pnl_realized: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class AnalysisProvider(Protocol):
    async def combined_analysis(self, symbol: str, exchange: str, timeframe: str) -> dict: ...


class OptionsDataClient(Protocol):
    def get_options_contracts(
        self,
        underlying_symbols: list[str],
        contract_type: str,
        expiration_date_gte: str,
        expiration_date_lte: str,
        strike_price_gte: float | None = None,
        strike_price_lte: float | None = None,
        limit: int = 1000,
    ) -> list[dict | OptionContract]: ...

    def get_latest_option_quotes(self, symbols: list[str], feed: str = "indicative") -> dict[str, dict | OptionQuote]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _dte(expiration_date: str, today: date) -> int:
    return (_parse_date(expiration_date) - today).days


def _date_range(config: OptionsConfig, today: date) -> tuple[str, str]:
    return (
        (today + timedelta(days=config.min_dte)).isoformat(),
        (today + timedelta(days=config.max_dte)).isoformat(),
    )


def normalize_option_contract(raw: dict | OptionContract) -> OptionContract | None:
    if isinstance(raw, OptionContract):
        return raw
    if not isinstance(raw, dict):
        return None
    symbol = str(raw.get("symbol") or "").upper()
    underlying = str(raw.get("underlying_symbol") or raw.get("root_symbol") or "").upper()
    contract_type = str(raw.get("type") or raw.get("contract_type") or "").lower()
    expiration = str(raw.get("expiration_date") or "")
    strike = _as_float(raw.get("strike_price"), 0.0)
    if not symbol or not underlying or contract_type not in {"call", "put"} or not expiration or strike <= 0:
        return None
    return OptionContract(
        symbol=symbol,
        underlying_symbol=underlying,
        type=contract_type,
        expiration_date=expiration,
        strike_price=strike,
        open_interest=_as_float(raw.get("open_interest"), 0.0),
        tradable=bool(raw.get("tradable", True)),
    )


def normalize_option_quote(symbol: str, raw: dict | OptionQuote) -> OptionQuote | None:
    if isinstance(raw, OptionQuote):
        return raw
    if not isinstance(raw, dict):
        return None
    bid = _as_float(raw.get("bid", raw.get("bp")), 0.0)
    ask = _as_float(raw.get("ask", raw.get("ap")), 0.0)
    timestamp = raw.get("timestamp", raw.get("t"))
    if bid <= 0 or ask <= 0:
        return None
    return OptionQuote(symbol=symbol.upper(), bid=bid, ask=ask, timestamp=str(timestamp) if timestamp else None)


def _quote_mid(quote: OptionQuote) -> float:
    return (quote.bid + quote.ask) / 2.0


def _spread_pct(quote: OptionQuote) -> float:
    mid = _quote_mid(quote)
    if mid <= 0:
        return 999.0
    return (quote.ask - quote.bid) / mid


def filter_option_candidates(
    underlying_symbol: str,
    contract_type: str,
    *,
    underlying_price: float,
    signal_score: float,
    contracts: list[OptionContract | dict],
    quotes: dict[str, OptionQuote | dict],
    config: OptionsConfig,
    today: date | None = None,
) -> list[OptionCandidate]:
    today = today or date.today()
    underlying_symbol = underlying_symbol.upper()
    contract_type = contract_type.lower()
    candidates: list[OptionCandidate] = []

    for raw_contract in contracts:
        contract = normalize_option_contract(raw_contract)
        if contract is None:
            continue
        if contract.underlying_symbol != underlying_symbol or contract.type != contract_type:
            continue
        if not contract.tradable or contract.open_interest < config.min_open_interest:
            continue
        dte = _dte(contract.expiration_date, today)
        if dte < config.min_dte or dte > config.max_dte:
            continue
        if underlying_price > 0:
            low = underlying_price * (1 - config.strike_pct_window)
            high = underlying_price * (1 + config.strike_pct_window)
            if contract.strike_price < low or contract.strike_price > high:
                continue
        raw_quote = quotes.get(contract.symbol)
        if raw_quote is None:
            continue
        quote = normalize_option_quote(contract.symbol, raw_quote)
        if quote is None or quote.ask <= 0 or quote.bid <= 0 or quote.ask < quote.bid:
            continue
        spread_pct = _spread_pct(quote)
        if spread_pct > config.max_spread_pct:
            continue
        max_loss = quote.ask * 100 * config.contracts_per_trade
        if config.max_contract_debit > 0 and max_loss > config.max_contract_debit:
            continue
        strategy = "long call" if contract_type == "call" else "long put"
        candidates.append(
            OptionCandidate(
                underlying_symbol=underlying_symbol,
                contract_symbol=contract.symbol,
                strategy=strategy,
                contract_type=contract_type,
                expiration_date=contract.expiration_date,
                dte=dte,
                strike_price=contract.strike_price,
                bid=quote.bid,
                ask=quote.ask,
                mid=_quote_mid(quote),
                spread_pct=spread_pct,
                open_interest=contract.open_interest,
                contracts=config.contracts_per_trade,
                max_loss=max_loss,
                signal_score=signal_score,
                reasons=[
                    f"{dte} DTE within {config.min_dte}-{config.max_dte}",
                    f"spread {spread_pct * 100:.1f}% <= {config.max_spread_pct * 100:.1f}%",
                    f"open_interest {contract.open_interest:.0f} >= {config.min_open_interest:.0f}",
                ],
            )
        )

    target = config.target_dte
    if contract_type == "call":
        strike_distance = lambda c: abs((c.strike_price - underlying_price) / underlying_price) if underlying_price > 0 else 0
    else:
        strike_distance = lambda c: abs((underlying_price - c.strike_price) / underlying_price) if underlying_price > 0 else 0
    return sorted(candidates, key=lambda c: (abs(c.dte - target), c.spread_pct, strike_distance(c), -c.open_interest, c.ask))


class OptionsPaperLedger:
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
            CREATE TABLE IF NOT EXISTS option_positions (
                contract_symbol TEXT PRIMARY KEY,
                underlying_symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                contract_type TEXT NOT NULL,
                strike_price REAL NOT NULL,
                expiration_date TEXT NOT NULL,
                contracts INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                entry_debit REAL NOT NULL,
                entry_time TEXT NOT NULL,
                last_mid REAL,
                highest_mid REAL,
                thesis TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS option_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_symbol TEXT NOT NULL,
                underlying_symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                contracts INTEGER NOT NULL,
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
        if self.conn.execute("SELECT value FROM state WHERE key='options_cash'").fetchone() is None:
            self.conn.execute("INSERT INTO state(key, value) VALUES('options_cash', ?)", (str(float(starting_cash)),))
        self.conn.commit()

    @property
    def cash(self) -> float:
        row = self.conn.execute("SELECT value FROM state WHERE key='options_cash'").fetchone()
        return float(row["value"])

    def _set_cash(self, value: float) -> None:
        self.conn.execute("UPDATE state SET value=? WHERE key='options_cash'", (str(float(value)),))

    def get_position(self, contract_symbol: str) -> OptionPosition | None:
        row = self.conn.execute(
            "SELECT * FROM option_positions WHERE contract_symbol=?", (contract_symbol.upper(),)
        ).fetchone()
        if row is None:
            return None
        return OptionPosition(
            contract_symbol=row["contract_symbol"],
            underlying_symbol=row["underlying_symbol"],
            strategy=row["strategy"],
            contract_type=row["contract_type"],
            strike_price=float(row["strike_price"]),
            expiration_date=row["expiration_date"],
            contracts=int(row["contracts"]),
            entry_price=float(row["entry_price"]),
            entry_debit=float(row["entry_debit"]),
            entry_time=row["entry_time"],
            last_mid=float(row["last_mid"]) if row["last_mid"] is not None else None,
            highest_mid=float(row["highest_mid"]) if row["highest_mid"] is not None else None,
            thesis=row["thesis"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def get_position_for_underlying(self, underlying_symbol: str) -> OptionPosition | None:
        row = self.conn.execute(
            "SELECT contract_symbol FROM option_positions WHERE underlying_symbol=? ORDER BY contract_symbol LIMIT 1",
            (underlying_symbol.upper(),),
        ).fetchone()
        if row is None:
            return None
        return self.get_position(row["contract_symbol"])

    def list_positions(self) -> list[OptionPosition]:
        rows = self.conn.execute("SELECT contract_symbol FROM option_positions ORDER BY underlying_symbol, contract_symbol").fetchall()
        positions = [self.get_position(row["contract_symbol"]) for row in rows]
        return [position for position in positions if position is not None]

    def open_position_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM option_positions").fetchone()
        return int(row["n"])

    def buy_to_open(self, candidate: OptionCandidate, reason: str, metadata: dict[str, Any]) -> OptionTrade:
        contract_symbol = candidate.contract_symbol.upper()
        if self.get_position(contract_symbol) is not None:
            raise ValueError(f"option position for {contract_symbol} is already open")
        if candidate.ask <= 0 or candidate.max_loss <= 0:
            raise ValueError("option candidate ask and max_loss must be positive")
        if candidate.max_loss > self.cash + 1e-9:
            raise ValueError("not enough options paper cash")
        timestamp = _now()
        cash_after = self.cash - candidate.max_loss
        stored_metadata = dict(metadata)
        stored_metadata.update(
            {
                "signal_score": candidate.signal_score,
                "bid_at_entry": candidate.bid,
                "ask_at_entry": candidate.ask,
                "spread_pct": candidate.spread_pct,
                "open_interest": candidate.open_interest,
            }
        )
        self.conn.execute(
            """
            INSERT INTO option_positions(
                contract_symbol, underlying_symbol, strategy, contract_type, strike_price, expiration_date,
                contracts, entry_price, entry_debit, entry_time, last_mid, highest_mid, thesis, metadata_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contract_symbol,
                candidate.underlying_symbol,
                candidate.strategy,
                candidate.contract_type,
                candidate.strike_price,
                candidate.expiration_date,
                candidate.contracts,
                candidate.ask,
                candidate.max_loss,
                timestamp,
                candidate.mid,
                candidate.mid,
                reason,
                json.dumps(stored_metadata, sort_keys=True),
            ),
        )
        self._set_cash(cash_after)
        trade_id = self._insert_trade(
            contract_symbol,
            candidate.underlying_symbol,
            "BUY_TO_OPEN",
            candidate.contracts,
            candidate.ask,
            candidate.max_loss,
            reason,
            timestamp,
            cash_after,
            0.0,
            stored_metadata,
        )
        self.conn.commit()
        return OptionTrade(
            trade_id,
            contract_symbol,
            candidate.underlying_symbol,
            "BUY_TO_OPEN",
            candidate.contracts,
            candidate.ask,
            candidate.max_loss,
            reason,
            timestamp,
            cash_after,
            0.0,
            stored_metadata,
        )

    def sell_to_close(self, contract_symbol: str, price: float, reason: str, metadata: dict[str, Any]) -> OptionTrade:
        contract_symbol = contract_symbol.upper()
        position = self.get_position(contract_symbol)
        if position is None:
            raise ValueError(f"no open option position for {contract_symbol}")
        if price <= 0:
            raise ValueError("option close price must be positive")
        proceeds = price * 100 * position.contracts
        pnl = proceeds - position.entry_debit
        cash_after = self.cash + proceeds
        timestamp = _now()
        self.conn.execute("DELETE FROM option_positions WHERE contract_symbol=?", (contract_symbol,))
        self._set_cash(cash_after)
        trade_id = self._insert_trade(
            contract_symbol,
            position.underlying_symbol,
            "SELL_TO_CLOSE",
            position.contracts,
            price,
            proceeds,
            reason,
            timestamp,
            cash_after,
            pnl,
            metadata,
        )
        self.conn.commit()
        return OptionTrade(
            trade_id,
            contract_symbol,
            position.underlying_symbol,
            "SELL_TO_CLOSE",
            position.contracts,
            price,
            proceeds,
            reason,
            timestamp,
            cash_after,
            pnl,
            metadata,
        )

    def mark_mid(self, contract_symbol: str, mid: float) -> None:
        position = self.get_position(contract_symbol)
        if position is None or mid <= 0:
            return
        highest = max(position.highest_mid or position.entry_price, mid)
        self.conn.execute(
            "UPDATE option_positions SET last_mid=?, highest_mid=? WHERE contract_symbol=?",
            (mid, highest, contract_symbol.upper()),
        )
        self.conn.commit()

    def _insert_trade(
        self,
        contract_symbol: str,
        underlying_symbol: str,
        side: str,
        contracts: int,
        price: float,
        notional: float,
        reason: str,
        timestamp: str,
        cash_after: float,
        pnl_realized: float,
        metadata: dict[str, Any],
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO option_trades(
                contract_symbol, underlying_symbol, side, contracts, price, notional, reason,
                timestamp, cash_after, pnl_realized, metadata_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contract_symbol,
                underlying_symbol,
                side,
                contracts,
                price,
                notional,
                reason,
                timestamp,
                cash_after,
                pnl_realized,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        return int(cur.lastrowid)

    def list_trades(self) -> list[OptionTrade]:
        rows = self.conn.execute("SELECT * FROM option_trades ORDER BY id").fetchall()
        return [
            OptionTrade(
                id=int(row["id"]),
                contract_symbol=row["contract_symbol"],
                underlying_symbol=row["underlying_symbol"],
                side=row["side"],
                contracts=int(row["contracts"]),
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


def _watch_exchange_map(config: BotConfig) -> dict[str, str]:
    return {item.symbol.upper(): item.exchange.upper() for item in config.watchlist}


def _underlyings(config: BotConfig) -> list[WatchItem]:
    exchange_by_symbol = _watch_exchange_map(config)
    result: list[WatchItem] = []
    seen: set[str] = set()
    for symbol in config.options.underlying_symbols[: config.options.max_underlyings_per_scan]:
        symbol = symbol.upper()
        if symbol in seen:
            continue
        seen.add(symbol)
        result.append(WatchItem(symbol=symbol, exchange=exchange_by_symbol.get(symbol, "NASDAQ")))
    return result


def _direction_for_analysis(config: BotConfig, analysis: dict, signal_action: str) -> tuple[str | None, float, list[str], float]:
    score, reasons, price, rsi = score_analysis(analysis)
    if signal_action == "BUY" and config.options.allow_calls:
        return "call", score, reasons, price
    if score <= config.strategy.exit_score and config.options.allow_puts:
        return "put", score, [f"Score {score:.1f} <= bearish put threshold {config.strategy.exit_score:.1f}"] + reasons, price
    return None, score, reasons, price


def _fetch_option_quotes(client: OptionsDataClient, symbols: list[str], feed: str) -> dict[str, OptionQuote]:
    quotes: dict[str, OptionQuote] = {}
    for start in range(0, len(symbols), 100):
        batch = symbols[start : start + 100]
        raw_quotes = client.get_latest_option_quotes(batch, feed=feed)
        for symbol, raw_quote in raw_quotes.items():
            quote = normalize_option_quote(symbol, raw_quote)
            if quote is not None:
                quotes[symbol.upper()] = quote
    return quotes


def _fetch_candidates_for_underlying(
    config: BotConfig,
    client: OptionsDataClient,
    item: WatchItem,
    contract_type: str,
    underlying_price: float,
    score: float,
    today: date,
) -> list[OptionCandidate]:
    exp_gte, exp_lte = _date_range(config.options, today)
    strike_gte = underlying_price * (1 - config.options.strike_pct_window) if underlying_price > 0 else None
    strike_lte = underlying_price * (1 + config.options.strike_pct_window) if underlying_price > 0 else None
    raw_contracts = client.get_options_contracts(
        [item.symbol],
        contract_type,
        exp_gte,
        exp_lte,
        strike_price_gte=strike_gte,
        strike_price_lte=strike_lte,
        limit=1000,
    )
    contracts = [contract for contract in (normalize_option_contract(raw) for raw in raw_contracts) if contract is not None]
    if not contracts:
        return []
    quote_symbols = [contract.symbol for contract in contracts]
    quotes = _fetch_option_quotes(client, quote_symbols, config.options.quote_feed)
    return filter_option_candidates(
        item.symbol,
        contract_type,
        underlying_price=underlying_price,
        signal_score=score,
        contracts=contracts,
        quotes=quotes,
        config=config.options,
        today=today,
    )


async def _scan_candidates(
    config: BotConfig,
    equity_ledger: PaperLedger,
    provider: AnalysisProvider,
    client: OptionsDataClient,
    today: date,
) -> tuple[list[OptionCandidate], list[str]]:
    candidates: list[OptionCandidate] = []
    lines: list[str] = []
    for item in _underlyings(config):
        analysis = await provider.combined_analysis(item.symbol, item.exchange, config.provider.timeframe)
        signal = generate_signal(
            symbol=item.symbol,
            exchange=item.exchange,
            analysis=analysis,
            position=equity_ledger.get_position(item.symbol),
            entry_score=config.strategy.entry_score,
            exit_score=config.strategy.exit_score,
            max_rsi_for_entry=config.strategy.max_rsi_for_entry,
            stop_loss_pct=config.strategy.stop_loss_pct,
            take_profit_pct=config.strategy.take_profit_pct,
        )
        contract_type, score, reasons, price = _direction_for_analysis(config, analysis, signal.action)
        if contract_type is None or price <= 0:
            lines.append(f"HOLD OPTIONS {item.symbol}: no options entry; score={score:.1f}; reason={signal.reasons[0]}")
            continue
        option_candidates = _fetch_candidates_for_underlying(config, client, item, contract_type, price, score, today)
        if not option_candidates:
            lines.append(f"SKIP OPTIONS {item.symbol}: no liquid {contract_type} candidate passed filters; score={score:.1f}")
            continue
        best = option_candidates[0]
        best.reasons = [signal.reasons[0]] + reasons[:3] + best.reasons
        candidates.append(best)
    candidates.sort(key=lambda candidate: (-candidate.signal_score, candidate.spread_pct, abs(candidate.dte - config.options.target_dte)))
    return candidates, lines


def _candidate_line(candidate: OptionCandidate) -> str:
    return (
        f"{candidate.underlying_symbol} {candidate.strategy} {candidate.contract_symbol} "
        f"strike={candidate.strike_price:.2f} exp={candidate.expiration_date} dte={candidate.dte} "
        f"bid={candidate.bid:.2f} ask={candidate.ask:.2f} spread={candidate.spread_pct * 100:.1f}% "
        f"max_loss={candidate.max_loss:.2f} score={candidate.signal_score:.1f}"
    )


async def options_scan_once(
    config: BotConfig,
    equity_ledger: PaperLedger,
    options_ledger: OptionsPaperLedger,
    provider: AnalysisProvider,
    client: OptionsDataClient,
    today: date | None = None,
) -> str:
    if not config.execution.dry_run or config.execution.live_trading_enabled:
        raise ValueError("refusing to run options scanner: paper trading only")
    today = today or date.today()
    lines = [
        "stonks-paper-bot options scan",
        "Boundary: READ-ONLY OPTIONS SCAN — no broker option orders submitted, no option ledger trades written",
        f"Options cash snapshot: {options_ledger.cash:.2f}",
    ]
    if not config.options.enabled:
        lines.append("Options overlay disabled in config; set [options].enabled=true to scan.")
        return "\n".join(lines)
    candidates, notes = await _scan_candidates(config, equity_ledger, provider, client, today)
    lines.extend(notes)
    if not candidates:
        lines.append("No options candidates passed filters.")
        return "\n".join(lines)
    for rank, candidate in enumerate(candidates[: config.selection.preview_top or len(candidates)], start=1):
        lines.append(f"#{rank} " + _candidate_line(candidate))
    return "\n".join(lines)


def _close_reason(config: BotConfig, position: OptionPosition, quote: OptionQuote, today: date) -> tuple[bool, str, float]:
    close_price = quote.bid
    if close_price <= 0:
        return False, "no usable bid", close_price
    dte = _dte(position.expiration_date, today)
    if close_price <= position.entry_price * (1 - config.options.stop_loss_pct):
        return True, f"option stop loss: bid {close_price:.2f} <= {position.entry_price * (1 - config.options.stop_loss_pct):.2f}", close_price
    if close_price >= position.entry_price * (1 + config.options.take_profit_pct):
        return True, f"option take profit: bid {close_price:.2f} >= {position.entry_price * (1 + config.options.take_profit_pct):.2f}", close_price
    if dte <= config.options.min_exit_dte:
        return True, f"expiration risk: {dte} DTE <= min_exit_dte {config.options.min_exit_dte}", close_price
    return False, "position remains within option risk rules", close_price


def _refresh_and_close_positions(config: BotConfig, options_ledger: OptionsPaperLedger, client: OptionsDataClient, today: date) -> list[str]:
    positions = options_ledger.list_positions()
    if not positions:
        return []
    raw_quotes = client.get_latest_option_quotes([position.contract_symbol for position in positions], feed=config.options.quote_feed)
    lines: list[str] = []
    for position in positions:
        quote = normalize_option_quote(position.contract_symbol, raw_quotes.get(position.contract_symbol, {}))
        if quote is None:
            lines.append(f"HOLD OPTIONS {position.contract_symbol}: no usable quote; no local close recorded")
            continue
        options_ledger.mark_mid(position.contract_symbol, _quote_mid(quote))
        should_close, reason, close_price = _close_reason(config, position, quote, today)
        if should_close:
            trade = options_ledger.sell_to_close(
                position.contract_symbol,
                price=close_price,
                reason=reason,
                metadata={"bid": quote.bid, "ask": quote.ask},
            )
            lines.append(
                f"PAPER OPTIONS SELL_TO_CLOSE {trade.contract_symbol}: contracts={trade.contracts} @ {trade.price:.2f}, "
                f"proceeds={trade.notional:.2f}, realized_pnl={trade.pnl_realized:.2f}, reason={reason}"
            )
        else:
            pnl = close_price * 100 * position.contracts - position.entry_debit
            lines.append(
                f"OPTIONS HOLD {position.contract_symbol}: bid={close_price:.2f}, unrealized≈{pnl:.2f}, reason={reason}"
            )
    return lines


async def options_paper_once(
    config: BotConfig,
    equity_ledger: PaperLedger,
    options_ledger: OptionsPaperLedger,
    provider: AnalysisProvider,
    client: OptionsDataClient,
    today: date | None = None,
) -> str:
    if not config.execution.dry_run or config.execution.live_trading_enabled:
        raise ValueError("refusing to run options paper loop: paper trading only")
    today = today or date.today()
    lines = [
        "stonks-paper-bot options paper",
        "Boundary: LOCAL OPTIONS PAPER ONLY — no Alpaca option orders submitted, no live execution",
    ]
    if not config.options.enabled:
        lines.append("Options overlay disabled in config; set [options].enabled=true to paper trade.")
        return "\n".join(lines)

    lines.extend(_refresh_and_close_positions(config, options_ledger, client, today))
    candidates, notes = await _scan_candidates(config, equity_ledger, provider, client, today)
    lines.extend(notes)
    open_underlyings = {position.underlying_symbol for position in options_ledger.list_positions()}
    risk_cap = config.starting_cash * config.options.max_trade_risk_pct

    for candidate in candidates:
        if options_ledger.open_position_count() >= config.options.max_open_positions:
            lines.append(f"SKIP OPTIONS {candidate.underlying_symbol}: max open option positions reached")
            continue
        if candidate.underlying_symbol in open_underlyings:
            lines.append(f"SKIP OPTIONS {candidate.underlying_symbol}: option position already open for underlying")
            continue
        if risk_cap > 0 and candidate.max_loss > risk_cap:
            lines.append(
                f"SKIP OPTIONS {candidate.underlying_symbol}: max_loss={candidate.max_loss:.2f} exceeds risk_cap={risk_cap:.2f}"
            )
            continue
        try:
            trade = options_ledger.buy_to_open(
                candidate,
                reason="; ".join(candidate.reasons[:5]),
                metadata={"score": candidate.signal_score, "strategy": candidate.strategy},
            )
        except ValueError as exc:
            lines.append(f"SKIP OPTIONS {candidate.underlying_symbol}: {exc}")
            continue
        open_underlyings.add(candidate.underlying_symbol)
        lines.append(
            f"PAPER OPTIONS BUY_TO_OPEN {trade.underlying_symbol} {trade.contract_symbol}: contracts={trade.contracts} "
            f"@ {trade.price:.2f}, debit={trade.notional:.2f}, max_loss={trade.notional:.2f}, score={candidate.signal_score:.1f}"
        )
    lines.append(format_options_status(config, options_ledger, compact=True))
    return "\n".join(lines)


def format_options_status(config: BotConfig, options_ledger: OptionsPaperLedger, compact: bool = False) -> str:
    positions = options_ledger.list_positions()
    trades = options_ledger.list_trades()
    realized = sum(trade.pnl_realized for trade in trades if trade.side == "SELL_TO_CLOSE")
    position_value = sum((position.last_mid or position.entry_price) * 100 * position.contracts for position in positions)
    equity = options_ledger.cash + position_value
    lines = [
        f"Options portfolio: cash={options_ledger.cash:.2f}, open_positions={len(positions)}, equity≈{equity:.2f}, realized_pnl={realized:.2f}",
    ]
    if not compact:
        lines.insert(0, "Boundary: LOCAL OPTIONS PAPER ONLY — no broker option orders, no live execution")
        lines.append(
            f"Options rules: {config.options.min_dte}-{config.options.max_dte} DTE, "
            f"max_spread={config.options.max_spread_pct * 100:.1f}%, max_debit={config.options.max_contract_debit:.2f}, "
            f"risk_cap={config.options.max_trade_risk_pct * 100:.2f}% of starting cash"
        )
    for position in positions:
        mark = position.last_mid or position.entry_price
        value = mark * 100 * position.contracts
        pnl = value - position.entry_debit
        pct = (mark / position.entry_price - 1) * 100 if position.entry_price else 0.0
        lines.append(
            f"OPTIONS PAPER OPEN {position.underlying_symbol} {position.strategy} {position.contract_symbol}: "
            f"contracts={position.contracts}, entry={position.entry_price:.2f}, mark={mark:.2f}, "
            f"entry_debit={position.entry_debit:.2f}, max_loss={position.entry_debit:.2f}, "
            f"unrealized≈{pnl:.2f} ({pct:.1f}%), exp={position.expiration_date}"
        )
    if not positions and not compact:
        lines.append("No open options paper positions.")
    if not compact:
        lines.append(f"Option trades recorded: {len(trades)}")
    return "\n".join(lines)
