from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from subprocess import run
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import BotConfig, load_config
from .ledger import PaperLedger

PAPER_BOUNDARY = "PAPER ONLY — no broker orders, no live execution"
DEFAULT_SERVICE_NAMES = ["stonks-paper-bot.service", "stonks-paper-dashboard.service"]


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _pct(value: float) -> str:
    return f"{value:+.2f}%"


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def _systemctl_user_status(service_name: str) -> dict[str, str]:
    try:
        result = run(
            ["systemctl", "--user", "is-active", service_name],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        active = (result.stdout or result.stderr).strip() or "unknown"
        return {"name": service_name, "active": active}
    except Exception as exc:  # pragma: no cover - defensive around host systemd state
        return {"name": service_name, "active": f"unknown: {type(exc).__name__}"}


def collect_dashboard_data(config: BotConfig, ledger: PaperLedger, service_names: list[str] | None = None) -> dict[str, Any]:
    positions = ledger.list_positions()
    trades = ledger.list_trades()
    realized_pnl = sum(trade.pnl_realized for trade in trades if trade.side == "SELL")
    position_value = sum((position.last_price or position.entry_price) * position.quantity for position in positions)
    cost_basis = sum(position.entry_price * position.quantity for position in positions)
    unrealized_pnl = position_value - cost_basis
    equity = ledger.cash + position_value
    total_return_pct = ((equity / config.starting_cash) - 1) * 100 if config.starting_cash else 0.0

    position_rows = []
    for position in positions:
        mark = position.last_price or position.entry_price
        highest = position.highest_price or mark
        pnl = (mark - position.entry_price) * position.quantity
        pnl_pct = ((mark / position.entry_price) - 1) * 100 if position.entry_price else 0.0
        drawdown_pct = ((mark / highest) - 1) * 100 if highest else 0.0
        position_rows.append(
            {
                "symbol": position.symbol,
                "exchange": position.exchange,
                "quantity": _round(position.quantity, 6),
                "entry_price": _round(position.entry_price),
                "mark_price": _round(mark),
                "highest_price": _round(highest),
                "entry_time": position.entry_time,
                "notional": _round(position.entry_price * position.quantity),
                "market_value": _round(mark * position.quantity),
                "unrealized_pnl": _round(pnl),
                "pnl_pct": _round(pnl_pct),
                "drawdown_from_peak_pct": _round(drawdown_pct),
                "thesis": position.thesis,
                "score": position.metadata.get("score"),
            }
        )

    trade_rows = [
        {
            "id": trade.id,
            "symbol": trade.symbol,
            "exchange": trade.exchange,
            "side": trade.side,
            "quantity": _round(trade.quantity, 6),
            "price": _round(trade.price),
            "notional": _round(trade.notional),
            "reason": trade.reason,
            "timestamp": trade.timestamp,
            "cash_after": _round(trade.cash_after),
            "pnl_realized": _round(trade.pnl_realized),
            "score": trade.metadata.get("score"),
        }
        for trade in reversed(trades[-25:])
    ]

    services = [_systemctl_user_status(name) for name in service_names] if service_names is not None else [_systemctl_user_status(name) for name in DEFAULT_SERVICE_NAMES]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "boundary": PAPER_BOUNDARY,
        "portfolio": {
            "cash": _round(ledger.cash),
            "position_value": _round(position_value),
            "equity": _round(equity),
            "starting_cash": _round(config.starting_cash),
            "realized_pnl": _round(realized_pnl),
            "unrealized_pnl": _round(unrealized_pnl),
            "total_return_pct": _round(total_return_pct),
            "open_positions": len(positions),
            "max_open_positions": config.max_open_positions,
            "trades_recorded": len(trades),
        },
        "positions": position_rows,
        "recent_trades": trade_rows,
        "watchlist": [{"symbol": item.symbol, "exchange": item.exchange} for item in config.watchlist],
        "strategy": {
            "entry_score": config.strategy.entry_score,
            "exit_score": config.strategy.exit_score,
            "max_rsi_for_entry": config.strategy.max_rsi_for_entry,
            "stop_loss_pct": config.strategy.stop_loss_pct,
            "take_profit_pct": config.strategy.take_profit_pct,
            "max_position_pct": config.max_position_pct,
            "scan_interval_seconds": config.execution.scan_interval_seconds,
            "timeframe": config.provider.timeframe,
        },
        "services": services,
    }


def _position_card(position: dict[str, Any]) -> str:
    pnl_class = "good" if position["unrealized_pnl"] >= 0 else "bad"
    score = "—" if position.get("score") is None else escape(str(position["score"]))
    return f"""
    <article class="card position-card">
      <div class="row"><h3>{escape(position['symbol'])}</h3><span class="badge">{escape(position['exchange'])}</span></div>
      <div class="metric {pnl_class}">{_money(position['unrealized_pnl'])} <small>{_pct(position['pnl_pct'])}</small></div>
      <dl>
        <div><dt>Qty</dt><dd>{position['quantity']}</dd></div>
        <div><dt>Entry</dt><dd>{_money(position['entry_price'])}</dd></div>
        <div><dt>Mark</dt><dd>{_money(position['mark_price'])}</dd></div>
        <div><dt>Value</dt><dd>{_money(position['market_value'])}</dd></div>
        <div><dt>Peak DD</dt><dd>{_pct(position['drawdown_from_peak_pct'])}</dd></div>
        <div><dt>Score</dt><dd>{score}</dd></div>
      </dl>
      <p class="muted">{escape(position.get('thesis') or 'No thesis recorded.')}</p>
    </article>
    """


def _trade_row(trade: dict[str, Any]) -> str:
    side_class = "buy" if trade["side"] == "BUY" else "sell"
    pnl = "" if trade["side"] == "BUY" else f" / PnL {_money(trade['pnl_realized'])}"
    return f"""
    <tr>
      <td>#{trade['id']}</td>
      <td><strong>{escape(trade['symbol'])}</strong></td>
      <td><span class="pill {side_class}">{escape(trade['side'])}</span></td>
      <td>{trade['quantity']} @ {_money(trade['price'])}{pnl}</td>
      <td>{escape(trade['timestamp'])}</td>
      <td>{escape(trade['reason'])}</td>
    </tr>
    """


def render_dashboard_html(data: dict[str, Any], api_path: str = "/api/dashboard") -> str:
    portfolio = data["portfolio"]
    equity_class = "good" if portfolio["total_return_pct"] >= 0 else "bad"
    service_html = "".join(
        f"<span class=\"service {escape(service['active'])}\">{escape(service['name'])}: {escape(service['active'])}</span>"
        for service in data.get("services", [])
    ) or "<span class=\"muted\">Service state not checked</span>"
    positions_html = "".join(_position_card(position) for position in data["positions"]) or '<article class="card"><h3>No open paper positions</h3><p class="muted">The autonomous loop is watching for qualifying entries.</p></article>'
    trades_html = "".join(_trade_row(trade) for trade in data["recent_trades"]) or '<tr><td colspan="6" class="muted">No paper trades recorded yet.</td></tr>'
    watchlist = ", ".join(f"{escape(item['symbol'])}:{escape(item['exchange'])}" for item in data["watchlist"])
    embedded = json.dumps(data, sort_keys=True)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stonks Paper Bot Dashboard</title>
  <style>
    :root {{ color-scheme: dark; --bg:#080b12; --panel:#111827; --panel2:#172033; --text:#e5e7eb; --muted:#9ca3af; --line:#263244; --green:#34d399; --red:#fb7185; --yellow:#fbbf24; --blue:#60a5fa; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font:15px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background: radial-gradient(circle at top left, #1d2b4f 0, transparent 30rem), var(--bg); color:var(--text); }}
    main {{ max-width:1180px; margin:0 auto; padding:24px; }}
    header {{ display:flex; flex-wrap:wrap; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:20px; }}
    h1, h2, h3, p {{ margin-top:0; }}
    h1 {{ font-size:clamp(28px, 5vw, 48px); letter-spacing:-0.04em; margin-bottom:6px; }}
    h2 {{ margin:26px 0 12px; }}
    .boundary {{ color:#111827; background:var(--yellow); border-radius:999px; padding:8px 12px; font-weight:800; display:inline-flex; }}
    .grid {{ display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:14px; }}
    .positions {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:14px; }}
    .card {{ background:linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.018)); border:1px solid var(--line); border-radius:20px; padding:18px; box-shadow:0 18px 45px rgba(0,0,0,.25); }}
    .label {{ color:var(--muted); font-size:13px; text-transform:uppercase; letter-spacing:.08em; }}
    .metric {{ font-size:28px; font-weight:850; letter-spacing:-0.03em; }}
    small, .muted {{ color:var(--muted); }}
    .good {{ color:var(--green); }} .bad {{ color:var(--red); }}
    .row {{ display:flex; justify-content:space-between; gap:12px; align-items:center; }}
    .badge, .pill, .service {{ border:1px solid var(--line); border-radius:999px; padding:4px 9px; color:var(--muted); background:rgba(255,255,255,.03); }}
    .service.active {{ color:var(--green); border-color:rgba(52,211,153,.45); }}
    .service.inactive, .service.failed {{ color:var(--red); border-color:rgba(251,113,133,.45); }}
    .services {{ display:flex; gap:8px; flex-wrap:wrap; margin:12px 0; }}
    dl {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:10px 16px; margin:16px 0 0; }}
    dt {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }} dd {{ margin:2px 0 0; font-weight:700; }}
    table {{ width:100%; border-collapse:collapse; overflow:hidden; border-radius:14px; }}
    th, td {{ border-bottom:1px solid var(--line); padding:12px; text-align:left; vertical-align:top; }}
    th {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
    .buy {{ color:var(--green); }} .sell {{ color:var(--red); }}
    code {{ color:var(--blue); }}
    @media (max-width: 820px) {{ main {{ padding:16px; }} .grid {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }} table {{ display:block; overflow-x:auto; }} }}
    @media (max-width: 520px) {{ .grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Stonks Paper Bot</h1>
      <p class="muted">Generated <span id="generated">{escape(data['generated_at'])}</span>. Watchlist: {watchlist}. API: <code>{escape(api_path)}</code></p>
      <div class="services">{service_html}</div>
    </div>
    <div class="boundary">{escape(data['boundary'])}</div>
  </header>

  <section class="grid" aria-label="Portfolio">
    <article class="card"><div class="label">Equity</div><div class="metric {equity_class}">{_money(portfolio['equity'])}</div><small>{_pct(portfolio['total_return_pct'])} total return</small></article>
    <article class="card"><div class="label">Cash</div><div class="metric">{_money(portfolio['cash'])}</div><small>Starting cash {_money(portfolio['starting_cash'])}</small></article>
    <article class="card"><div class="label">Open Position Value</div><div class="metric">{_money(portfolio['position_value'])}</div><small>{portfolio['open_positions']} / {portfolio['max_open_positions']} open</small></article>
    <article class="card"><div class="label">PnL</div><div class="metric {equity_class}">{_money(portfolio['realized_pnl'] + portfolio['unrealized_pnl'])}</div><small>Realized {_money(portfolio['realized_pnl'])} · Unrealized {_money(portfolio['unrealized_pnl'])}</small></article>
  </section>

  <h2>Open Positions</h2>
  <section class="positions">{positions_html}</section>

  <h2>Strategy</h2>
  <section class="grid">
    <article class="card"><div class="label">Entry / Exit</div><div class="metric">{data['strategy']['entry_score']:.0f} / {data['strategy']['exit_score']:.0f}</div><small>Score thresholds</small></article>
    <article class="card"><div class="label">Risk</div><div class="metric">{data['strategy']['max_position_pct'] * 100:.0f}%</div><small>Max per-position allocation</small></article>
    <article class="card"><div class="label">Stops</div><div class="metric">-{data['strategy']['stop_loss_pct'] * 100:.0f}% / +{data['strategy']['take_profit_pct'] * 100:.0f}%</div><small>Stop loss / take profit</small></article>
    <article class="card"><div class="label">Cadence</div><div class="metric">{int(data['strategy']['scan_interval_seconds'] / 60)}m</div><small>{escape(data['strategy']['timeframe'])} analysis timeframe</small></article>
  </section>

  <h2>Recent Paper Trades</h2>
  <article class="card"><table><thead><tr><th>ID</th><th>Symbol</th><th>Side</th><th>Fill</th><th>Time</th><th>Reason</th></tr></thead><tbody>{trades_html}</tbody></table></article>

  <p class="muted">Read-only dashboard. It reads the local SQLite paper ledger only; it does not call market-data providers or submit broker orders.</p>
</main>
<script id="dashboard-data" type="application/json">{escape(embedded)}</script>
<script>
  const token = new URLSearchParams(location.search).get('token');
  const apiUrl = new URL({json.dumps(api_path)}, location.href);
  if (token) apiUrl.searchParams.set('token', token);
  setInterval(() => fetch(apiUrl).then(r => r.ok ? r.json() : null).then(data => {{
    if (data && data.generated_at) document.getElementById('generated').textContent = data.generated_at;
  }}).catch(() => {{}}), 60000);
</script>
</body>
</html>"""


def render_login_html() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Stonks Dashboard Login</title><style>body{font:16px system-ui;background:#080b12;color:#e5e7eb;display:grid;place-items:center;min-height:100vh;margin:0}.box{background:#111827;border:1px solid #263244;border-radius:20px;padding:24px;max-width:420px}input{width:100%;padding:12px;border-radius:10px;border:1px solid #263244;background:#0b1020;color:#e5e7eb}button{margin-top:12px;padding:10px 14px;border:0;border-radius:10px;background:#fbbf24;color:#111827;font-weight:800}</style></head><body><form class="box" method="get"><h1>Stonks Dashboard</h1><p>Enter the dashboard token.</p><input name="token" type="password" autofocus><button type="submit">Open dashboard</button></form></body></html>"""


def _is_authorized(handler: BaseHTTPRequestHandler) -> bool:
    token = os.environ.get("STONKS_DASHBOARD_TOKEN", "").strip()
    if not token:
        return True
    auth = handler.headers.get("Authorization", "")
    if auth == f"Bearer {token}":
        return True
    query_token = parse_qs(urlparse(handler.path).query).get("token", [""])[0]
    return query_token == token


class DashboardServer(ThreadingHTTPServer):
    config_path: Path


def make_handler(config_path: Path):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "StonksPaperDashboard/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}", flush=True)

        def _send(self, status: HTTPStatus, body: str | bytes, content_type: str) -> None:
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _data(self) -> dict[str, Any]:
            config = load_config(config_path)
            ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
            try:
                return collect_dashboard_data(config, ledger)
            finally:
                ledger.close()

        def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send(HTTPStatus.OK, "ok\n", "text/plain; charset=utf-8")
                return
            if parsed.path not in {"/", "/api/dashboard"}:
                self._send(HTTPStatus.NOT_FOUND, "not found\n", "text/plain; charset=utf-8")
                return
            if not _is_authorized(self):
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("WWW-Authenticate", "Bearer")
                body = render_login_html().encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                data = self._data()
            except Exception as exc:
                payload = {"error": f"{type(exc).__name__}: {exc}"}
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, json.dumps(payload), "application/json; charset=utf-8")
                return
            if parsed.path == "/api/dashboard":
                self._send(HTTPStatus.OK, json.dumps(data, indent=2, sort_keys=True), "application/json; charset=utf-8")
            else:
                self._send(HTTPStatus.OK, render_dashboard_html(data), "text/html; charset=utf-8")

    return DashboardHandler


def serve_dashboard(config_path: Path, host: str = "127.0.0.1", port: int = 8791) -> None:
    token_state = "enabled" if os.environ.get("STONKS_DASHBOARD_TOKEN", "").strip() else "disabled"
    print(f"Starting stonks paper dashboard on http://{host}:{port} (auth={token_state}, boundary={PAPER_BOUNDARY})", flush=True)
    server = DashboardServer((host, port), make_handler(config_path))
    try:
        server.serve_forever()
    finally:
        server.server_close()
