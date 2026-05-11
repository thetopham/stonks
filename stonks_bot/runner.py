from __future__ import annotations

from typing import Protocol

from .config import BotConfig
from .ledger import PaperLedger
from .strategy import generate_signal


class AnalysisProvider(Protocol):
    async def combined_analysis(self, symbol: str, exchange: str, timeframe: str) -> dict: ...


class BrokerFillLike(Protocol):
    order_id: str
    symbol: str
    side: str
    status: str
    quantity: float
    price: float
    notional: float


class BrokerExecutor(Protocol):
    def market_is_open(self) -> tuple[bool, str]: ...
    def submit_buy(self, symbol: str, notional: float) -> BrokerFillLike: ...
    def submit_sell(self, symbol: str, quantity: float) -> BrokerFillLike: ...


def _execution_price(price: float, side: str, slippage_pct: float) -> float:
    slip = slippage_pct / 100.0
    if side == "BUY":
        return price * (1 + slip)
    if side == "SELL":
        return price * (1 - slip)
    return price


def _boundary_line(config: BotConfig) -> str:
    if config.broker.submit_orders:
        return "Boundary: ALPACA PAPER BROKER ORDERS ENABLED — paper endpoint only, no real-money endpoint"
    return "Boundary: LOCAL PAPER ONLY — no broker orders, no live execution"


def _broker_metadata(fill: BrokerFillLike, score: float) -> dict:
    return {
        "score": score,
        "broker_order_id": fill.order_id,
        "broker_status": fill.status,
        "broker_side": fill.side,
    }


async def run_once(config: BotConfig, ledger: PaperLedger, provider: AnalysisProvider, broker: BrokerExecutor | None = None) -> str:
    if not config.execution.dry_run or config.execution.live_trading_enabled:
        raise ValueError("refusing to run: paper trading only")
    if config.broker.submit_orders and broker is None:
        raise ValueError("broker order submission is enabled but no broker executor was provided")

    lines = ["stonks-paper-bot scan", _boundary_line(config)]
    market_state: tuple[bool, str] | None = None

    def broker_market_state() -> tuple[bool, str]:
        nonlocal market_state
        if market_state is None:
            assert broker is not None
            market_state = broker.market_is_open()
        return market_state

    for item in config.watchlist:
        analysis = await provider.combined_analysis(item.symbol, item.exchange, config.provider.timeframe)
        position = ledger.get_position(item.symbol)
        signal = generate_signal(
            symbol=item.symbol,
            exchange=item.exchange,
            analysis=analysis,
            position=position,
            entry_score=config.strategy.entry_score,
            exit_score=config.strategy.exit_score,
            max_rsi_for_entry=config.strategy.max_rsi_for_entry,
            stop_loss_pct=config.strategy.stop_loss_pct,
            take_profit_pct=config.strategy.take_profit_pct,
        )

        if signal.action == "BUY":
            if ledger.open_position_count() >= config.max_open_positions:
                lines.append(f"HOLD {item.symbol}: max open positions reached")
                continue
            notional = min(ledger.cash * config.max_position_pct, ledger.cash)
            if config.broker.submit_orders:
                is_open, market_reason = broker_market_state()
                if not is_open:
                    lines.append(f"BROKER HOLD {item.symbol}: {market_reason}; no Alpaca paper order submitted")
                    continue
                assert broker is not None
                fill = broker.submit_buy(item.symbol, notional)
                trade = ledger.buy(
                    item.symbol,
                    item.exchange,
                    price=fill.price,
                    notional=fill.notional,
                    reason="; ".join(signal.reasons[:4]),
                    metadata=_broker_metadata(fill, signal.score),
                )
                lines.append(
                    f"ALPACA PAPER BUY {trade.symbol}: order_id={fill.order_id}, qty={trade.quantity:.4f} @ {trade.price:.2f}, "
                    f"notional={trade.notional:.2f}, score={signal.score:.1f}"
                )
            else:
                price = _execution_price(signal.price, "BUY", config.slippage_pct)
                trade = ledger.buy(item.symbol, item.exchange, price=price, notional=notional, reason="; ".join(signal.reasons[:4]), metadata={"score": signal.score})
                lines.append(f"PAPER BUY {trade.symbol}: qty={trade.quantity:.4f} @ {trade.price:.2f}, notional={trade.notional:.2f}, score={signal.score:.1f}")
        elif signal.action == "SELL":
            if config.broker.submit_orders:
                is_open, market_reason = broker_market_state()
                if not is_open:
                    lines.append(f"BROKER HOLD {item.symbol}: {market_reason}; no Alpaca paper sell submitted")
                    ledger.mark_price(item.symbol, signal.price)
                    continue
                assert broker is not None
                current_position = ledger.get_position(item.symbol)
                if current_position is None:
                    lines.append(f"HOLD {item.symbol}: sell signal but no local paper position")
                    continue
                fill = broker.submit_sell(item.symbol, current_position.quantity)
                trade = ledger.sell(item.symbol, price=fill.price, reason="; ".join(signal.reasons[:4]), metadata=_broker_metadata(fill, signal.score))
                lines.append(
                    f"ALPACA PAPER SELL {trade.symbol}: order_id={fill.order_id}, qty={trade.quantity:.4f} @ {trade.price:.2f}, "
                    f"pnl={trade.pnl_realized:.2f}, score={signal.score:.1f}"
                )
            else:
                price = _execution_price(signal.price, "SELL", config.slippage_pct)
                trade = ledger.sell(item.symbol, price=price, reason="; ".join(signal.reasons[:4]), metadata={"score": signal.score})
                lines.append(f"PAPER SELL {trade.symbol}: qty={trade.quantity:.4f} @ {trade.price:.2f}, pnl={trade.pnl_realized:.2f}, score={signal.score:.1f}")
        else:
            ledger.mark_price(item.symbol, signal.price)
            lines.append(f"HOLD {item.symbol}: price={signal.price:.2f}, score={signal.score:.1f}, reason={signal.reasons[0]}")

    lines.append(format_status(config, ledger, compact=True))
    return "\n".join(lines)


def format_status(config: BotConfig, ledger: PaperLedger, compact: bool = False) -> str:
    positions = ledger.list_positions()
    trades = ledger.list_trades()
    realized = sum(trade.pnl_realized for trade in trades if trade.side == "SELL")
    position_value = sum((position.last_price or position.entry_price) * position.quantity for position in positions)
    equity = ledger.cash + position_value

    lines = [
        f"Portfolio: cash={ledger.cash:.2f}, open_positions={len(positions)}, equity≈{equity:.2f}, realized_pnl={realized:.2f}",
    ]
    if not compact:
        lines.insert(0, _boundary_line(config))
    if positions:
        for position in positions:
            mark = position.last_price or position.entry_price
            pnl = (mark - position.entry_price) * position.quantity
            pct = ((mark / position.entry_price) - 1) * 100 if position.entry_price else 0
            broker_order = position.metadata.get("broker_order_id")
            broker_suffix = f", broker_order_id={broker_order}" if broker_order else ""
            lines.append(f"PAPER OPEN {position.symbol}: qty={position.quantity:.4f}, entry={position.entry_price:.2f}, mark={mark:.2f}, unrealized={pnl:.2f} ({pct:.2f}%){broker_suffix}")
    elif not compact:
        lines.append("No open paper positions.")
    if not compact:
        lines.append(f"Trades recorded: {len(trades)}")
    return "\n".join(lines)
