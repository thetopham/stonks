from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .config import BotConfig
from .ledger import PaperLedger
from .models import Position, WatchItem, default_crypto_broker_symbol
from .screener import build_static_candidate_universe, merge_candidate_universes
from .strategy import Signal, generate_signal


class AnalysisProvider(Protocol):
    async def combined_analysis(self, symbol: str, exchange: str, timeframe: str) -> dict: ...


class CandidateDiscoveryProvider(AnalysisProvider, Protocol):
    last_discovery_notes: list[str]

    async def discover_candidates(
        self,
        exchanges: list[str],
        timeframe: str,
        sources: list[str],
        per_source_limit: int,
    ) -> list[WatchItem]: ...


class BrokerFillLike(Protocol):
    order_id: str
    symbol: str
    side: str
    status: str
    quantity: float
    price: float
    notional: float


class BrokerExecutor(Protocol):
    def market_is_open(self, *, extended_hours: bool = False) -> tuple[bool, str]: ...
    def submit_buy(self, symbol: str, notional: float) -> BrokerFillLike: ...
    def submit_sell(self, symbol: str, quantity: float) -> BrokerFillLike: ...
    def submit_extended_hours_buy(self, symbol: str, notional: float, limit_price: float, time_in_force: str) -> BrokerFillLike: ...
    def submit_extended_hours_sell(self, symbol: str, quantity: float, limit_price: float, time_in_force: str) -> BrokerFillLike: ...
    def submit_crypto_buy(self, symbol: str, notional: float) -> BrokerFillLike: ...
    def submit_crypto_sell(self, symbol: str, quantity: float) -> BrokerFillLike: ...


@dataclass(slots=True)
class CandidateEvaluation:
    item: WatchItem
    signal: Signal
    position: Position | None
    portfolio_score: float


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


def _broker_symbol(item: WatchItem) -> str | None:
    if item.asset_class != "crypto":
        return item.symbol
    return item.broker_symbol or default_crypto_broker_symbol(item.symbol)


def _metadata_for_item(item: WatchItem) -> dict:
    metadata = {"asset_class": item.asset_class}
    broker_symbol = _broker_symbol(item)
    if broker_symbol and broker_symbol != item.symbol:
        metadata["broker_symbol"] = broker_symbol
    return metadata


def _broker_metadata(fill: BrokerFillLike, score: float, portfolio_score: float, item: WatchItem | None = None) -> dict:
    metadata = {
        "score": score,
        "portfolio_score": portfolio_score,
        "broker_order_id": fill.order_id,
        "broker_status": fill.status,
        "broker_side": fill.side,
    }
    if item is not None:
        metadata.update(_metadata_for_item(item))
    return metadata


def _local_metadata(evaluation: CandidateEvaluation) -> dict:
    metadata = {"score": evaluation.signal.score, "portfolio_score": evaluation.portfolio_score}
    metadata.update(_metadata_for_item(evaluation.item))
    return metadata


def _rsi_extension_risk(signal: Signal) -> float:
    if signal.rsi is None:
        return 0.0
    return max(0.0, signal.rsi - 60.0)


def _portfolio_adjusted_score(config: BotConfig, ledger: PaperLedger, item: WatchItem, signal: Signal) -> float:
    score = signal.score
    if not config.optimizer.enabled:
        return score

    same_exchange_positions = sum(1 for position in ledger.list_positions() if position.exchange == item.exchange.upper())
    diversification_penalty = min(10.0, same_exchange_positions * 2.0)
    return max(0.0, score - diversification_penalty)


def _refresh_portfolio_scores(config: BotConfig, ledger: PaperLedger, evaluations: list[CandidateEvaluation]) -> None:
    for evaluation in evaluations:
        evaluation.portfolio_score = _portfolio_adjusted_score(config, ledger, evaluation.item, evaluation.signal)


def _candidate_sort_key(evaluation: CandidateEvaluation) -> tuple[float, float, int, float, str]:
    return (
        -evaluation.portfolio_score,
        -evaluation.signal.score,
        -len(evaluation.signal.reasons),
        _rsi_extension_risk(evaluation.signal),
        evaluation.item.symbol,
    )


def _ordered_buy_candidates(config: BotConfig, evaluations: list[CandidateEvaluation]) -> list[CandidateEvaluation]:
    candidates = [evaluation for evaluation in evaluations if evaluation.signal.action == "BUY"]
    if config.selection.mode == "ranked":
        return sorted(candidates, key=_candidate_sort_key)
    return candidates


def _ordered_screen_rows(config: BotConfig, evaluations: list[CandidateEvaluation]) -> list[CandidateEvaluation]:
    if config.selection.mode == "ranked":
        return sorted(evaluations, key=_candidate_sort_key)
    return evaluations


def _buy_notional(config: BotConfig, ledger: PaperLedger) -> float:
    if ledger.cash <= 0:
        return 0.0
    base_notional = min(ledger.cash * config.max_position_pct, ledger.cash)
    if not config.optimizer.enabled:
        return base_notional
    reserve_cash = config.starting_cash * config.optimizer.cash_reserve_pct
    spendable_cash = max(0.0, ledger.cash - reserve_cash)
    return min(base_notional, spendable_cash, ledger.cash)


async def _candidate_universe(config: BotConfig, ledger: PaperLedger, provider: AnalysisProvider) -> tuple[list[WatchItem], list[str]]:
    open_items = [
        WatchItem(
            position.symbol,
            position.exchange,
            str(position.metadata.get("asset_class") or "auto"),
            str(position.metadata.get("broker_symbol") or "") or None,
        )
        for position in ledger.list_positions()
    ]
    open_symbols = {item.symbol for item in open_items}
    static_items = build_static_candidate_universe(config, cap=False)
    discovery_notes: list[str] = []
    dynamic_items: list[WatchItem] = []

    if config.screener.enabled and config.screener.source in {"mcp", "hybrid"}:
        discover = getattr(provider, "discover_candidates", None)
        if callable(discover):
            dynamic_items = await discover(
                config.screener.exchanges,
                config.provider.timeframe,
                config.screener.dynamic_sources,
                config.screener.per_source_limit,
            )
            discovery_notes = list(getattr(provider, "last_discovery_notes", []))
        else:
            discovery_notes = ["mcp-discovery=unavailable-on-provider"]

    if config.screener.enabled and config.screener.source in {"mcp", "hybrid"}:
        # Dynamic MCP results are the primary broad-market source. The watchlist
        # remains a fallback/seed, but it should not consume the deep-analysis cap
        # before scanner-discovered opportunities get a chance to rank.
        merged = merge_candidate_universes(config, open_items, dynamic_items, static_items, cap=True, protected_symbols=open_symbols)
    else:
        merged = merge_candidate_universes(config, open_items, static_items, cap=True, protected_symbols=open_symbols)
    return merged, discovery_notes


async def _evaluate_candidates(config: BotConfig, ledger: PaperLedger, provider: AnalysisProvider) -> tuple[list[CandidateEvaluation], list[str]]:
    evaluations: list[CandidateEvaluation] = []
    candidates, discovery_notes = await _candidate_universe(config, ledger, provider)
    for item in candidates:
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
        evaluations.append(
            CandidateEvaluation(
                item=item,
                signal=signal,
                position=position,
                portfolio_score=_portfolio_adjusted_score(config, ledger, item, signal),
            )
        )
    return evaluations, discovery_notes


def _rank_preview(config: BotConfig, candidates: list[CandidateEvaluation]) -> str | None:
    if not candidates or config.selection.preview_top <= 0:
        return None
    preview = []
    for rank, evaluation in enumerate(candidates[: config.selection.preview_top], start=1):
        preview.append(
            f"#{rank} {evaluation.item.symbol} score={evaluation.signal.score:.1f} adj={evaluation.portfolio_score:.1f} price={evaluation.signal.price:.2f}"
        )
    label = "Ranked candidates" if config.selection.mode == "ranked" else "Sequential candidates"
    return f"{label}: " + "; ".join(preview)


def _hold_line(evaluation: CandidateEvaluation) -> str:
    return f"HOLD {evaluation.item.symbol}: price={evaluation.signal.price:.2f}, score={evaluation.signal.score:.1f}, reason={evaluation.signal.reasons[0]}"


def _execute_sell(
    config: BotConfig,
    ledger: PaperLedger,
    evaluation: CandidateEvaluation,
    broker: BrokerExecutor | None,
    broker_market_state,
) -> str:
    signal = evaluation.signal
    if config.broker.submit_orders:
        assert broker is not None
        current_position = ledger.get_position(evaluation.item.symbol)
        if current_position is None:
            return f"HOLD {evaluation.item.symbol}: sell signal but no local paper position"

        if evaluation.item.asset_class == "crypto":
            broker_symbol = _broker_symbol(evaluation.item)
            if not broker_symbol:
                ledger.mark_price(evaluation.item.symbol, signal.price)
                return f"BROKER HOLD {evaluation.item.symbol}: no Alpaca crypto broker_symbol configured; no paper sell submitted"
            fill = broker.submit_crypto_sell(broker_symbol, current_position.quantity)
            trade = ledger.sell(
                evaluation.item.symbol,
                price=fill.price,
                reason="; ".join(signal.reasons[:4]),
                metadata=_broker_metadata(fill, signal.score, evaluation.portfolio_score, evaluation.item),
            )
            return (
                f"ALPACA PAPER CRYPTO SELL {trade.symbol}: broker_symbol={broker_symbol}, order_id={fill.order_id}, "
                f"qty={trade.quantity:.4f} @ {trade.price:.2f}, pnl={trade.pnl_realized:.2f}, "
                f"score={signal.score:.1f}, adj={evaluation.portfolio_score:.1f}"
            )

        is_open, market_reason = broker_market_state(config.broker.equity_extended_hours)
        if not is_open:
            ledger.mark_price(evaluation.item.symbol, signal.price)
            return f"BROKER HOLD {evaluation.item.symbol}: {market_reason}; no Alpaca paper sell submitted"
        if config.broker.equity_extended_hours:
            limit_price = _execution_price(signal.price, "SELL", config.slippage_pct)
            fill = broker.submit_extended_hours_sell(
                evaluation.item.symbol,
                current_position.quantity,
                limit_price,
                config.broker.equity_extended_hours_time_in_force,
            )
            trade = ledger.sell(
                evaluation.item.symbol,
                price=fill.price,
                reason="; ".join(signal.reasons[:4]),
                metadata=_broker_metadata(fill, signal.score, evaluation.portfolio_score, evaluation.item),
            )
            return (
                f"ALPACA PAPER 24/5 SELL {trade.symbol}: order_id={fill.order_id}, limit={limit_price:.2f}, "
                f"qty={trade.quantity:.4f} @ {trade.price:.2f}, pnl={trade.pnl_realized:.2f}, "
                f"score={signal.score:.1f}, adj={evaluation.portfolio_score:.1f}"
            )
        fill = broker.submit_sell(evaluation.item.symbol, current_position.quantity)
        trade = ledger.sell(
            evaluation.item.symbol,
            price=fill.price,
            reason="; ".join(signal.reasons[:4]),
            metadata=_broker_metadata(fill, signal.score, evaluation.portfolio_score, evaluation.item),
        )
        return (
            f"ALPACA PAPER SELL {trade.symbol}: order_id={fill.order_id}, qty={trade.quantity:.4f} @ {trade.price:.2f}, "
            f"pnl={trade.pnl_realized:.2f}, score={signal.score:.1f}, adj={evaluation.portfolio_score:.1f}"
        )

    price = _execution_price(signal.price, "SELL", config.slippage_pct)
    trade = ledger.sell(evaluation.item.symbol, price=price, reason="; ".join(signal.reasons[:4]), metadata=_local_metadata(evaluation))
    return f"PAPER SELL {trade.symbol}: qty={trade.quantity:.4f} @ {trade.price:.2f}, pnl={trade.pnl_realized:.2f}, score={signal.score:.1f}, adj={evaluation.portfolio_score:.1f}"


def _execute_buy(
    config: BotConfig,
    ledger: PaperLedger,
    evaluation: CandidateEvaluation,
    notional: float,
    broker: BrokerExecutor | None,
    broker_market_state,
) -> tuple[bool, str]:
    signal = evaluation.signal
    if config.broker.submit_orders:
        assert broker is not None
        if evaluation.item.asset_class == "crypto":
            broker_symbol = _broker_symbol(evaluation.item)
            if not broker_symbol:
                return False, f"BROKER HOLD {evaluation.item.symbol}: no Alpaca crypto broker_symbol configured; no paper order submitted"
            fill = broker.submit_crypto_buy(broker_symbol, notional)
            trade = ledger.buy(
                evaluation.item.symbol,
                evaluation.item.exchange,
                price=fill.price,
                notional=fill.notional,
                reason="; ".join(signal.reasons[:4]),
                metadata=_broker_metadata(fill, signal.score, evaluation.portfolio_score, evaluation.item),
            )
            return True, (
                f"ALPACA PAPER CRYPTO BUY {trade.symbol}: broker_symbol={broker_symbol}, order_id={fill.order_id}, "
                f"qty={trade.quantity:.4f} @ {trade.price:.2f}, notional={trade.notional:.2f}, "
                f"score={signal.score:.1f}, adj={evaluation.portfolio_score:.1f}"
            )

        is_open, market_reason = broker_market_state(config.broker.equity_extended_hours)
        if not is_open:
            return False, f"BROKER HOLD {evaluation.item.symbol}: {market_reason}; no Alpaca paper order submitted"
        if config.broker.equity_extended_hours:
            limit_price = _execution_price(signal.price, "BUY", config.slippage_pct)
            fill = broker.submit_extended_hours_buy(
                evaluation.item.symbol,
                notional,
                limit_price,
                config.broker.equity_extended_hours_time_in_force,
            )
            trade = ledger.buy(
                evaluation.item.symbol,
                evaluation.item.exchange,
                price=fill.price,
                notional=fill.notional,
                reason="; ".join(signal.reasons[:4]),
                metadata=_broker_metadata(fill, signal.score, evaluation.portfolio_score, evaluation.item),
            )
            return True, (
                f"ALPACA PAPER 24/5 BUY {trade.symbol}: order_id={fill.order_id}, limit={limit_price:.2f}, "
                f"qty={trade.quantity:.4f} @ {trade.price:.2f}, notional={trade.notional:.2f}, "
                f"score={signal.score:.1f}, adj={evaluation.portfolio_score:.1f}"
            )
        fill = broker.submit_buy(evaluation.item.symbol, notional)
        trade = ledger.buy(
            evaluation.item.symbol,
            evaluation.item.exchange,
            price=fill.price,
            notional=fill.notional,
            reason="; ".join(signal.reasons[:4]),
            metadata=_broker_metadata(fill, signal.score, evaluation.portfolio_score, evaluation.item),
        )
        return True, (
            f"ALPACA PAPER BUY {trade.symbol}: order_id={fill.order_id}, qty={trade.quantity:.4f} @ {trade.price:.2f}, "
            f"notional={trade.notional:.2f}, score={signal.score:.1f}, adj={evaluation.portfolio_score:.1f}"
        )

    price = _execution_price(signal.price, "BUY", config.slippage_pct)
    trade = ledger.buy(
        evaluation.item.symbol,
        evaluation.item.exchange,
        price=price,
        notional=notional,
        reason="; ".join(signal.reasons[:4]),
        metadata=_local_metadata(evaluation),
    )
    return True, f"PAPER BUY {trade.symbol}: qty={trade.quantity:.4f} @ {trade.price:.2f}, notional={trade.notional:.2f}, score={signal.score:.1f}, adj={evaluation.portfolio_score:.1f}"


async def screen_once(config: BotConfig, ledger: PaperLedger, provider: AnalysisProvider) -> str:
    if not config.execution.dry_run or config.execution.live_trading_enabled:
        raise ValueError("refusing to run: paper trading only")

    evaluations, discovery_notes = await _evaluate_candidates(config, ledger, provider)
    rows = _ordered_screen_rows(config, evaluations)
    lines = [
        "stonks-paper-bot screener",
        "Boundary: READ-ONLY SCREENER — no broker orders submitted, no ledger trades written",
        _boundary_line(config),
        f"Screener source: {config.screener.source}; exchanges={','.join(config.screener.exchanges)}; max_deep_candidates={config.screener.max_candidates}",
        f"Candidates scored: {len(rows)}",
    ]
    if discovery_notes:
        lines.append("Discovery: " + "; ".join(discovery_notes[:12]))
    limit = config.selection.preview_top
    if limit > 0:
        for rank, evaluation in enumerate(rows[:limit], start=1):
            lines.append(
                f"#{rank} {evaluation.item.symbol}:{evaluation.item.exchange} {evaluation.signal.action} "
                f"score={evaluation.signal.score:.1f} adj={evaluation.portfolio_score:.1f} price={evaluation.signal.price:.2f} "
                f"reason={evaluation.signal.reasons[0]}"
            )
    return "\n".join(lines)


async def run_once(config: BotConfig, ledger: PaperLedger, provider: AnalysisProvider, broker: BrokerExecutor | None = None) -> str:
    if not config.execution.dry_run or config.execution.live_trading_enabled:
        raise ValueError("refusing to run: paper trading only")
    if config.broker.submit_orders and broker is None:
        raise ValueError("broker order submission is enabled but no broker executor was provided")

    lines = ["stonks-paper-bot scan", _boundary_line(config)]
    market_states: dict[bool, tuple[bool, str]] = {}

    def broker_market_state(extended_hours: bool = False) -> tuple[bool, str]:
        if extended_hours not in market_states:
            assert broker is not None
            market_states[extended_hours] = broker.market_is_open(extended_hours=extended_hours)
        return market_states[extended_hours]

    evaluations, discovery_notes = await _evaluate_candidates(config, ledger, provider)
    if discovery_notes:
        lines.append("Discovery: " + "; ".join(discovery_notes[:12]))

    for evaluation in evaluations:
        if evaluation.signal.action == "SELL":
            lines.append(_execute_sell(config, ledger, evaluation, broker, broker_market_state))

    for evaluation in evaluations:
        if evaluation.signal.action == "HOLD":
            ledger.mark_price(evaluation.item.symbol, evaluation.signal.price)
            lines.append(_hold_line(evaluation))

    _refresh_portfolio_scores(config, ledger, evaluations)
    buy_candidates = [evaluation for evaluation in evaluations if evaluation.signal.action == "BUY"]
    preview_line = _rank_preview(config, _ordered_buy_candidates(config, evaluations))
    if preview_line is not None:
        lines.append(preview_line)

    new_buys = 0
    while buy_candidates:
        _refresh_portfolio_scores(config, ledger, buy_candidates)
        if config.selection.mode == "ranked":
            buy_candidates.sort(key=_candidate_sort_key)
        evaluation = buy_candidates.pop(0)
        if ledger.get_position(evaluation.item.symbol) is not None:
            lines.append(f"SKIP {evaluation.item.symbol}: position already open")
            continue
        if ledger.open_position_count() >= config.max_open_positions:
            lines.append(f"SKIP {evaluation.item.symbol}: max open positions reached")
            continue
        if config.optimizer.enabled and new_buys >= config.optimizer.max_new_buys_per_scan:
            lines.append(f"SKIP {evaluation.item.symbol}: optimizer max new buys per scan reached")
            continue
        notional = _buy_notional(config, ledger)
        if notional < (config.optimizer.min_position_notional if config.optimizer.enabled else 0.01):
            reason = "optimizer cash reserve reached" if config.optimizer.enabled else "not enough paper cash"
            lines.append(f"SKIP {evaluation.item.symbol}: {reason}")
            continue
        bought, line = _execute_buy(config, ledger, evaluation, notional, broker, broker_market_state)
        lines.append(line)
        if bought:
            new_buys += 1

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
        lines.append(
            "Optimizer: "
            f"enabled={config.optimizer.enabled}, cash_reserve={config.optimizer.cash_reserve_pct * 100:.1f}%, "
            f"max_new_buys_per_scan={config.optimizer.max_new_buys_per_scan}, selection={config.selection.mode}, "
            f"screener_source={config.screener.source}"
        )
    if positions:
        for position in positions:
            mark = position.last_price or position.entry_price
            pnl = (mark - position.entry_price) * position.quantity
            pct = ((mark / position.entry_price) - 1) * 100 if position.entry_price else 0
            broker_order = position.metadata.get("broker_order_id")
            broker_suffix = f", broker_order_id={broker_order}" if broker_order else ""
            score = position.metadata.get("score")
            portfolio_score = position.metadata.get("portfolio_score")
            score_suffix = ""
            if score is not None:
                score_suffix = f", score={float(score):.1f}"
            if portfolio_score is not None:
                score_suffix += f", adj={float(portfolio_score):.1f}"
            lines.append(
                f"PAPER OPEN {position.symbol}: qty={position.quantity:.4f}, entry={position.entry_price:.2f}, mark={mark:.2f}, "
                f"unrealized={pnl:.2f} ({pct:.2f}%){score_suffix}{broker_suffix}"
            )
    elif not compact:
        lines.append("No open paper positions.")
    if not compact:
        lines.append(f"Trades recorded: {len(trades)}")
    return "\n".join(lines)
