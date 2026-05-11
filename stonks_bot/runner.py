from __future__ import annotations

from typing import Protocol

from .config import BotConfig
from .ledger import PaperLedger
from .models import WatchItem
from .strategy import generate_signal


class AnalysisProvider(Protocol):
    async def combined_analysis(self, symbol: str, exchange: str, timeframe: str) -> dict: ...


def _execution_price(price: float, side: str, slippage_pct: float) -> float:
    slip = slippage_pct / 100.0
    if side == "BUY":
        return price * (1 + slip)
    if side == "SELL":
        return price * (1 - slip)
    return price


async def run_once(config: BotConfig, ledger: PaperLedger, provider: AnalysisProvider) -> str:
    if not config.execution.dry_run or config.execution.live_trading_enabled:
        raise ValueError("refusing to run: paper trading only")

    lines = ["stonks-paper-bot dry-run scan", "Boundary: PAPER ONLY — no broker orders, no live execution"]
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
            price = _execution_price(signal.price, "BUY", config.slippage_pct)
            trade = ledger.buy(item.symbol, item.exchange, price=price, notional=notional, reason="; ".join(signal.reasons[:4]), metadata={"score": signal.score})
            lines.append(f"PAPER BUY {trade.symbol}: qty={trade.quantity:.4f} @ {trade.price:.2f}, notional={trade.notional:.2f}, score={signal.score:.1f}")
        elif signal.action == "SELL":
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
        lines.insert(0, "Boundary: PAPER ONLY — no broker orders, no live execution")
    if positions:
        for position in positions:
            mark = position.last_price or position.entry_price
            pnl = (mark - position.entry_price) * position.quantity
            pct = ((mark / position.entry_price) - 1) * 100 if position.entry_price else 0
            lines.append(f"PAPER OPEN {position.symbol}: qty={position.quantity:.4f}, entry={position.entry_price:.2f}, mark={mark:.2f}, unrealized={pnl:.2f} ({pct:.2f}%)")
    elif not compact:
        lines.append("No open paper positions.")
    if not compact:
        lines.append(f"Trades recorded: {len(trades)}")
    return "\n".join(lines)
