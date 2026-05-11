from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import BotConfig, load_config
from .dashboard import collect_dashboard_data
from .ledger import PaperLedger

DEFAULT_WIKI_PATH = Path(os.environ.get("HERMES_BLOOMBERG_WIKI_PATH", "/home/matt/wiki")).expanduser()
DEFAULT_BOT_ROOT = Path(os.environ.get("HERMES_BLOOMBERG_BOT_ROOT", "/home/matt/workspace/stonks-paper-bot")).expanduser()
DEFAULT_OUTPUT_DIR = Path(os.environ.get("HERMES_BLOOMBERG_OUTPUT_DIR", str(Path.home() / ".hermes" / "data" / "hermes-bloomberg"))).expanduser()

READ_ONLY_BOUNDARY = "READ ONLY / PAPER ONLY — collector reads local wiki, config, ledger, and optional public quote/news context; no broker orders, no live execution, no fund movement"
PRIORITY_WIKI_PAGES = [
    "projects/hermes-bloomberg-terminal.md",
    "projects/ai-trading-automation.md",
    "projects/demerzel-sidekick-v1.md",
    "concepts/llm-wiki-exocortex.md",
    "concepts/agentic-systems.md",
]
MAX_RECENT_LOG_ENTRIES = 5
MAX_LATEST_DAILY = 4
MAX_RECENT_FILES = 12
MAX_RECENT_TRADES = 10


def clean_text(value: Any, limit: int = 360) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def safe_read(path: Path, limit_chars: int | None = None) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if limit_chars and len(text) > limit_chars:
        return text[:limit_chars].rstrip() + "\n...[truncated by collector]..."
    return text


def relpath(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def first_heading(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return clean_text(line[2:].strip(), 120)
    return fallback


def metadata_field(text: str, name: str) -> str:
    match = re.search(rf"^\*\*{re.escape(name)}\*\*:\s*(.+)$", text, flags=re.MULTILINE)
    return clean_text(match.group(1), 160) if match else ""


def section_lines(text: str, names: tuple[str, ...]) -> list[str]:
    wanted = tuple(name.lower() for name in names)
    collecting = False
    start_level = 0
    lines: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^(#{2,6})\s+(.+?)\s*$", line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip().lower()
            if collecting and level <= start_level:
                break
            if not collecting and any(title.startswith(name) for name in wanted):
                collecting = True
                start_level = level
                continue
        elif collecting:
            lines.append(line)
    return lines


def section_excerpt(text: str, names: tuple[str, ...], limit: int = 420) -> str:
    filtered: list[str] = []
    in_code = False
    for line in section_lines(text, names):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not stripped:
            continue
        filtered.append(stripped)
    return clean_text(" ".join(filtered), limit)


def bullet_items(text: str, names: tuple[str, ...], max_items: int = 5, limit: int = 220) -> list[str]:
    items: list[str] = []
    for line in section_lines(text, names):
        match = re.match(r"^\s*(?:[-*]\s+|\d+\.\s+)(.+?)\s*$", line)
        if not match:
            continue
        item = clean_text(match.group(1), limit)
        if item and item not in items:
            items.append(item)
        if len(items) >= max_items:
            break
    return items


def wikilinks(text: str, max_items: int = 8) -> list[str]:
    links: list[str] = []
    for raw in re.findall(r"\[\[([^\]]+)\]\]", text):
        link = raw.split("|", 1)[0].strip()
        if link and link not in links:
            links.append(link)
        if len(links) >= max_items:
            break
    return links


def parse_index_counts(index_text: str) -> dict[str, int]:
    section_map = {"Daily Notes": "daily", "Projects": "projects", "Concepts": "concepts"}
    counts = {"daily": 0, "projects": 0, "concepts": 0}
    current = ""
    for line in index_text.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1).strip()
            continue
        if current in section_map:
            counts[section_map[current]] += len(re.findall(r"\[\[([^\]]+)\]\]", line))
    return counts


def parse_recent_log_entries(log_text: str, max_entries: int = MAX_RECENT_LOG_ENTRIES) -> list[dict[str, Any]]:
    matches = list(re.finditer(r"(?m)^##\s+(\d{4}-\d{2}-\d{2})([^\n]*)", log_text))
    entries: list[dict[str, Any]] = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(log_text)
        body = log_text[start:end]
        changed: list[str] = []
        notes: list[str] = []
        for code_span in re.findall(r"`([^`]+\.md)`", body):
            value = code_span.replace("\\", "/")
            if "/wiki/" in value:
                value = value.split("/wiki/", 1)[1]
            if value.startswith("wiki/"):
                value = value[5:]
            value = value.lstrip("./")
            if value and value not in changed:
                changed.append(value)
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            lower = stripped.lower()
            if lower.startswith(("- source:", "- source date:", "- ingested date:", "- created or updated:", "- created directory:")):
                continue
            note = clean_text(stripped[2:], 240)
            if note and note not in notes:
                notes.append(note)
            if len(notes) >= 4:
                break
        entries.append({"heading": clean_text((match.group(1) + match.group(2)).strip(), 160), "changed": changed[:12], "notes": notes})
    return entries[-max_entries:]


def run_git(root: Path, args: list[str], timeout: int = 4) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def git_snapshot(root: Path) -> dict[str, Any]:
    status = run_git(root, ["status", "--short"])
    return {
        "branch": run_git(root, ["branch", "--show-current"]),
        "head": run_git(root, ["rev-parse", "--short", "HEAD"]),
        "clean": not bool(status.strip()),
        "status_short": status.splitlines()[:20],
    }


def summarize_wiki_page(wiki_path: Path, rel: str) -> dict[str, Any] | None:
    path = wiki_path / rel
    if not path.exists() or not path.is_file():
        return None
    text = safe_read(path)
    return {
        "path": rel,
        "title": first_heading(text, Path(rel).stem),
        "type": metadata_field(text, "Type"),
        "source_date": metadata_field(text, "Source date"),
        "summary": section_excerpt(text, ("summary", "overview", "product shape"), 520),
        "status": section_excerpt(text, ("status", "current state", "execution boundary"), 420),
        "tasks": bullet_items(text, ("tasks", "near-term build path", "next steps", "open loops"), max_items=6),
        "open_questions": bullet_items(text, ("open questions", "questions"), max_items=6),
        "related": wikilinks("\n".join(section_lines(text, ("related pages",))) or text, max_items=8),
        "modified_utc": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
    }


def latest_daily_pages(wiki_path: Path, limit: int = MAX_LATEST_DAILY) -> list[str]:
    daily = wiki_path / "daily"
    if not daily.exists():
        return []
    pages = sorted((path for path in daily.glob("*.md") if path.is_file()), key=lambda path: path.name, reverse=True)
    return [relpath(path, wiki_path) for path in pages[:limit]]


def recent_maintained_files(wiki_path: Path, limit: int = MAX_RECENT_FILES) -> list[str]:
    paths: list[Path] = []
    for dirname in ("daily", "projects", "concepts"):
        directory = wiki_path / dirname
        if directory.exists():
            paths.extend(path for path in directory.glob("*.md") if path.is_file())
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [relpath(path, wiki_path) for path in paths[:limit]]


def collect_wiki_context(wiki_path: Path) -> dict[str, Any]:
    wiki_path = wiki_path.expanduser().resolve()
    index_text = safe_read(wiki_path / "index.md")
    log_text = safe_read(wiki_path / "log.md")
    schema_text = safe_read(wiki_path / "SCHEMA.md", limit_chars=8000)
    priority_pages = [page for rel in PRIORITY_WIKI_PAGES if (page := summarize_wiki_page(wiki_path, rel))]
    latest_daily = [page for rel in latest_daily_pages(wiki_path) if (page := summarize_wiki_page(wiki_path, rel))]
    return {
        "path": str(wiki_path),
        "exists": wiki_path.exists(),
        "counts": parse_index_counts(index_text),
        "git": git_snapshot(wiki_path) if (wiki_path / ".git").exists() else {},
        "schema_excerpt": clean_text(section_excerpt(schema_text, ("core rules", "style", "folder structure"), 520) or schema_text, 520),
        "recent_log_entries": parse_recent_log_entries(log_text),
        "priority_pages": priority_pages,
        "latest_daily_pages": latest_daily,
        "recent_files_by_mtime": recent_maintained_files(wiki_path),
    }


def resolve_config(config_path: Path) -> BotConfig:
    config_path = config_path.expanduser().resolve()
    config = load_config(config_path)
    if not config.ledger_path.is_absolute():
        config = replace(config, ledger_path=config_path.parent / config.ledger_path)
    return config


def collect_paper_bot_context(bot_root: Path, config_path: Path, check_services: bool = True) -> dict[str, Any]:
    bot_root = bot_root.expanduser().resolve()
    config_path = config_path.expanduser().resolve()
    config = resolve_config(config_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    try:
        data = collect_dashboard_data(config, ledger, service_names=None if check_services else [])
    finally:
        ledger.close()
    portfolio = dict(data["portfolio"])
    portfolio["watchlist_count"] = len(config.watchlist)
    return {
        "root": str(bot_root),
        "config_path": str(config_path),
        "ledger_path": str(config.ledger_path),
        "git": git_snapshot(bot_root) if (bot_root / ".git").exists() else {},
        "boundary": data["boundary"],
        "portfolio": portfolio,
        "positions": data["positions"],
        "recent_trades": data["recent_trades"][:MAX_RECENT_TRADES],
        "watchlist": data["watchlist"],
        "strategy": data["strategy"],
        "services": data.get("services", []),
    }


def stooq_symbol(symbol: str) -> str:
    value = symbol.strip().lower().replace("/", "-")
    if not value:
        return ""
    if "." not in value:
        value = f"{value}.us"
    return value


def stooq_quote_url(symbols: list[str]) -> str:
    params = urlencode({"s": ",".join(symbols), "f": "sd2t2ohlcv", "h": "", "e": "csv"})
    return f"https://stooq.com/q/l/?{params}"


def parse_float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip().upper() in {"", "N/D"}:
            return None
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_int(value: Any) -> int | None:
    parsed = parse_float(value)
    return int(parsed) if parsed is not None else None


def parse_stooq_csv_quotes(csv_text: str) -> list[dict[str, Any]]:
    quotes: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        raw_symbol = (row.get("Symbol") or "").strip().upper()
        if not raw_symbol:
            continue
        symbol = re.sub(r"\.[A-Z]+$", "", raw_symbol)
        open_price = parse_float(row.get("Open"))
        close_price = parse_float(row.get("Close"))
        if close_price is None:
            continue
        change_pct = None
        if open_price and open_price > 0:
            change_pct = round(((close_price / open_price) - 1) * 100, 2)
        date = (row.get("Date") or "").strip()
        time = (row.get("Time") or "").strip()
        market_time = f"{date}T{time}Z" if date and time and date.upper() != "N/D" and time.upper() != "N/D" else ""
        quotes.append(
            {
                "symbol": symbol,
                "name": "",
                "exchange": "STOOQ",
                "price": round(close_price, 4),
                "change_pct": change_pct,
                "volume": parse_int(row.get("Volume")),
                "market_time": market_time,
            }
        )
    return quotes


def fetch_stooq_quotes(symbols: list[str], timeout: int = 12) -> tuple[list[dict[str, Any]], list[str]]:
    clean_symbols: list[str] = []
    for symbol in symbols:
        value = stooq_symbol(symbol)
        if value and value not in clean_symbols:
            clean_symbols.append(value)
    if not clean_symbols:
        return [], []

    quotes: list[dict[str, Any]] = []
    errors: list[str] = []
    for symbol in clean_symbols:
        try:
            req = Request(stooq_quote_url([symbol]), headers={"User-Agent": "Hermes-Bloomberg-Collector/0.1"})
            with urlopen(req, timeout=timeout) as response:  # noqa: S310 - public quote endpoint, optional collector input
                csv_text = response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            errors.append(f"Stooq quote fetch failed for {symbol}: {type(exc).__name__}: {exc}")
            continue
        parsed = parse_stooq_csv_quotes(csv_text)
        if parsed:
            quotes.extend(parsed)
        else:
            errors.append(f"Stooq returned no usable quote for {symbol}")
    return quotes, errors


def google_news_rss_url(symbols: list[str]) -> str:
    query = " OR ".join(f"{symbol.upper()} stock" for symbol in symbols if symbol.strip()) or "stock market"
    params = urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    return f"https://news.google.com/rss/search?{params}"


def parse_rss_items(xml_text: str, max_items: int = 10) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        source = item.find("source")
        row = {
            "title": clean_text(item.findtext("title") or "", 220),
            "url": (item.findtext("link") or "").strip(),
            "published": clean_text(item.findtext("pubDate") or "", 120),
            "source": clean_text(source.text if source is not None else "", 120),
        }
        if row["title"] and row["url"]:
            items.append(row)
        if len(items) >= max_items:
            break
    return items


def fetch_google_news(symbols: list[str], symbol_limit: int = 8, timeout: int = 12) -> tuple[list[dict[str, str]], list[str]]:
    clean_symbols: list[str] = []
    for symbol in symbols[:symbol_limit]:
        value = symbol.strip().upper()
        if value and value not in clean_symbols:
            clean_symbols.append(value)
    try:
        req = Request(google_news_rss_url(clean_symbols), headers={"User-Agent": "Hermes-Bloomberg-Collector/0.1"})
        with urlopen(req, timeout=timeout) as response:  # noqa: S310 - public RSS endpoint, optional collector input
            xml_text = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        return [], [f"Google News RSS fetch failed: {type(exc).__name__}: {exc}"]
    items = parse_rss_items(xml_text, max_items=10)
    return items, [] if items else ["Google News RSS returned no usable items"]


def collect_market_snapshot(watchlist: list[dict[str, str]], include_quotes: bool, quote_limit: int) -> dict[str, Any]:
    if not include_quotes:
        return {
            "enabled": False,
            "quotes": [],
            "top_movers": [],
            "errors": [],
            "note": "Quote fetching disabled. Re-run with --quotes or HERMES_BLOOMBERG_QUOTES=1 for optional public Stooq quote context.",
        }
    symbols = [item["symbol"] for item in watchlist[:quote_limit] if item.get("symbol")]
    quotes, errors = fetch_stooq_quotes(symbols)
    top_movers = sorted(
        quotes,
        key=lambda quote: abs(float(quote.get("change_pct") or 0)),
        reverse=True,
    )[:10]
    return {
        "enabled": True,
        "quote_limit": quote_limit,
        "quotes": quotes,
        "top_movers": top_movers,
        "errors": errors,
        "note": "Public Stooq quote snapshot only; change_pct is latest close versus session open when prior close is unavailable; this collector still does not place trades or touch broker accounts.",
    }


def collect_news_snapshot(watchlist: list[dict[str, str]], include_news: bool, news_symbol_limit: int) -> dict[str, Any]:
    if not include_news:
        return {
            "enabled": False,
            "items": [],
            "errors": [],
            "note": "News fetching disabled. Re-run with --news or HERMES_BLOOMBERG_NEWS=1 for optional public Google News RSS context.",
        }
    symbols = [item["symbol"] for item in watchlist if item.get("symbol")]
    items, errors = fetch_google_news(symbols, symbol_limit=news_symbol_limit)
    return {
        "enabled": True,
        "symbol_limit": news_symbol_limit,
        "items": items,
        "errors": errors,
        "note": "Public Google News RSS context only; X/social ingestion remains disabled unless explicitly approved and credentialed.",
    }


def collect(
    wiki_path: str | Path = DEFAULT_WIKI_PATH,
    bot_root: str | Path = DEFAULT_BOT_ROOT,
    config_path: str | Path | None = None,
    include_quotes: bool = False,
    quote_limit: int = 25,
    include_news: bool = False,
    news_symbol_limit: int = 8,
    check_services: bool = True,
) -> dict[str, Any]:
    bot_root_path = Path(bot_root).expanduser()
    config_path_obj = Path(config_path).expanduser() if config_path is not None else bot_root_path / "config.paper.toml"
    wiki = collect_wiki_context(Path(wiki_path))
    paper_bot = collect_paper_bot_context(bot_root_path, config_path_obj, check_services=check_services)
    market_snapshot = collect_market_snapshot(paper_bot["watchlist"], include_quotes=include_quotes, quote_limit=quote_limit)
    news_snapshot = collect_news_snapshot(paper_bot["watchlist"], include_news=include_news, news_symbol_limit=news_symbol_limit)
    now = datetime.now().astimezone()
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "timezone": now.tzname(),
        "boundary": READ_ONLY_BOUNDARY,
        "wiki": wiki,
        "paper_bot": paper_bot,
        "market_snapshot": market_snapshot,
        "news_snapshot": news_snapshot,
        "collector_notes": [
            "Collector is read-only by default: no live trading, no broker setting changes, no private-key handling, no external posting.",
            "Personal context comes from the maintained wiki pages and recent daily/log entries, not raw transcript dumps.",
            "Paper bot state comes from the local SQLite ledger and dry-run config; ledger positions are simulated.",
            "Optional quotes/news are public context only and should not be treated as order instructions.",
            "Credentialed X/social ingestion is intentionally disabled in v0; add it later only with explicit API/source approval.",
        ],
        "briefing_questions": [
            "What moved?",
            "What changed in my own notes/theses?",
            "What are my paper positions doing?",
            "What signal or risk rule blocked trades?",
            "What old lesson from the wiki applies today?",
            "What is the one next move?",
        ],
    }


def money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def render_page(page: dict[str, Any]) -> list[str]:
    lines = [f"### `{page['path']}` — {page['title']}"]
    meta = []
    if page.get("type"):
        meta.append(f"type: {page['type']}")
    if page.get("modified_utc"):
        meta.append(f"modified UTC: {page['modified_utc']}")
    if meta:
        lines.append("- " + " | ".join(meta))
    if page.get("summary"):
        lines.append(f"- Summary: {page['summary']}")
    if page.get("status"):
        lines.append(f"- Status/boundary: {page['status']}")
    if page.get("tasks"):
        lines.append("- Tasks/next steps:")
        lines.extend(f"  - {item}" for item in page["tasks"][:6])
    if page.get("open_questions"):
        lines.append("- Open questions:")
        lines.extend(f"  - {item}" for item in page["open_questions"][:6])
    if page.get("related"):
        lines.append("- Related: " + ", ".join(f"[[{link}]]" for link in page["related"][:8]))
    return lines


def render_markdown(snapshot: dict[str, Any]) -> str:
    wiki = snapshot["wiki"]
    paper = snapshot["paper_bot"]
    portfolio = paper["portfolio"]
    market = snapshot["market_snapshot"]
    news = snapshot.get("news_snapshot", {"enabled": False, "items": [], "errors": [], "note": ""})
    lines: list[str] = [
        "# Hermes Bloomberg Terminal Collector Snapshot",
        "",
        f"Generated: {snapshot['generated_at']} ({snapshot.get('timezone')})",
        f"Boundary: {snapshot['boundary']}",
        f"Wiki path: `{wiki['path']}`",
        f"Paper bot root: `{paper['root']}`",
        "",
        "## Briefing questions this collector should support",
    ]
    lines.extend(f"- {question}" for question in snapshot.get("briefing_questions", []))
    lines.append("")

    lines.append("## Collector notes")
    lines.extend(f"- {note}" for note in snapshot.get("collector_notes", []))
    lines.append("")

    lines.append("## Wiki context")
    counts = wiki.get("counts", {})
    lines.append(f"- Counts: {counts.get('daily', 0)} daily notes; {counts.get('projects', 0)} projects; {counts.get('concepts', 0)} concepts")
    if wiki.get("git"):
        git = wiki["git"]
        status = "clean" if git.get("clean") else f"dirty ({len(git.get('status_short') or [])} shown changes)"
        lines.append(f"- Git: `{git.get('branch') or 'unknown'}` @ `{git.get('head') or 'unknown'}`; {status}")
    if wiki.get("schema_excerpt"):
        lines.append(f"- Schema excerpt: {wiki['schema_excerpt']}")
    lines.append("")

    lines.append("### Recent wiki log entries")
    for entry in wiki.get("recent_log_entries", []):
        lines.append(f"- {entry['heading']}")
        if entry.get("changed"):
            lines.append("  - Changed: " + ", ".join(f"`{path}`" for path in entry["changed"][:8]))
        if entry.get("notes"):
            for note in entry["notes"][:3]:
                lines.append(f"  - Note: {note}")
    lines.append("")

    lines.append("### Priority thesis/context pages")
    for page in wiki.get("priority_pages", []):
        lines.extend(render_page(page))
    lines.append("")

    lines.append("### Latest daily notes")
    for page in wiki.get("latest_daily_pages", []):
        lines.extend(render_page(page))
    lines.append("")

    lines.append("## Paper bot state")
    lines.append(f"- Boundary: {paper['boundary']}")
    lines.append(f"- Config: `{paper['config_path']}`")
    lines.append(f"- Ledger: `{paper['ledger_path']}`")
    lines.append(
        "- Portfolio: "
        f"cash={money(portfolio.get('cash'))}; "
        f"equity≈{money(portfolio.get('equity'))}; "
        f"realized={money(portfolio.get('realized_pnl'))}; "
        f"unrealized={money(portfolio.get('unrealized_pnl'))}; "
        f"return={pct(portfolio.get('total_return_pct'))}; "
        f"open={portfolio.get('open_positions')} / max {portfolio.get('max_open_positions')}; "
        f"watchlist={portfolio.get('watchlist_count')}"
    )
    strategy = paper.get("strategy", {})
    lines.append(
        "- Strategy gates: "
        f"entry_score≥{strategy.get('entry_score')}; exit_score≤{strategy.get('exit_score')}; "
        f"max_entry_rsi={strategy.get('max_rsi_for_entry')}; timeframe={strategy.get('timeframe')}; "
        f"stop_loss={pct((strategy.get('stop_loss_pct') or 0) * 100)}; take_profit={pct((strategy.get('take_profit_pct') or 0) * 100)}"
    )
    if paper.get("services"):
        services = ", ".join(f"{service['name']}={service['active']}" for service in paper["services"])
        lines.append(f"- Services: {services}")
    lines.append("")

    lines.append("### Open paper positions")
    if paper.get("positions"):
        for position in paper["positions"]:
            lines.append(
                f"- {position['symbol']} ({position['exchange']}): "
                f"mark={money(position.get('mark_price'))}; entry={money(position.get('entry_price'))}; "
                f"unrealized={money(position.get('unrealized_pnl'))} ({pct(position.get('pnl_pct'))}); "
                f"drawdown_from_peak={pct(position.get('drawdown_from_peak_pct'))}; score={position.get('score', '—')}"
            )
            if position.get("thesis"):
                lines.append(f"  - Thesis/reason: {position['thesis']}")
    else:
        lines.append("- No open paper positions.")
    lines.append("")

    lines.append("### Recent paper trades")
    if paper.get("recent_trades"):
        for trade in paper["recent_trades"]:
            pnl = f"; pnl={money(trade.get('pnl_realized'))}" if trade.get("side") == "SELL" else ""
            lines.append(
                f"- #{trade['id']} {trade['side']} {trade['symbol']}: "
                f"{trade['quantity']} @ {money(trade.get('price'))}; notional={money(trade.get('notional'))}{pnl}; reason={trade.get('reason', '')}"
            )
    else:
        lines.append("- No paper trades recorded.")
    lines.append("")

    lines.append("## Market quote snapshot")
    lines.append(f"- Enabled: {market.get('enabled')}")
    if market.get("note"):
        lines.append(f"- Note: {market['note']}")
    if market.get("errors"):
        for error in market["errors"]:
            lines.append(f"- Error: {error}")
    if market.get("top_movers"):
        lines.append("- Top movers from quoted watchlist:")
        for quote in market["top_movers"]:
            name = f" — {quote['name']}" if quote.get("name") else ""
            lines.append(f"  - {quote['symbol']}{name}: {money(quote.get('price'))}, {pct(quote.get('change_pct'))}, volume={quote.get('volume') or '—'}")
    lines.append("")

    lines.append("## Public news snapshot")
    lines.append(f"- Enabled: {news.get('enabled')}")
    if news.get("note"):
        lines.append(f"- Note: {news['note']}")
    if news.get("errors"):
        for error in news["errors"]:
            lines.append(f"- Error: {error}")
    if news.get("items"):
        for item in news["items"][:10]:
            source = f" — {item['source']}" if item.get("source") else ""
            published = f" ({item['published']})" if item.get("published") else ""
            lines.append(f"- [{item['title']}]({item['url']}){source}{published}")
    lines.append("")

    lines.append("## LLM synthesis instruction")
    lines.append("Use this as raw context for a short personal Bloomberg brief. Recommend one next move, cite local file paths as receipts, and keep the execution boundary explicit: read/report/recommend only unless Matt approves a separate action.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_outputs(snapshot: dict[str, Any], output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> dict[str, Path]:
    output_path = Path(output_dir).expanduser()
    output_path.mkdir(parents=True, exist_ok=True)
    generated = str(snapshot.get("generated_at", "")).split("T", 1)[0] or datetime.now().date().isoformat()
    latest_json = output_path / "latest_context.json"
    latest_markdown = output_path / "latest_context.md"
    dated_json = output_path / f"context-{generated}.json"
    dated_markdown = output_path / f"context-{generated}.md"
    content_json = json.dumps(snapshot, indent=2, sort_keys=True)
    content_markdown = render_markdown(snapshot)
    for path, content in (
        (latest_json, content_json + "\n"),
        (dated_json, content_json + "\n"),
        (latest_markdown, content_markdown),
        (dated_markdown, content_markdown),
    ):
        path.write_text(content, encoding="utf-8")
    return {
        "latest_json": latest_json,
        "latest_markdown": latest_markdown,
        "dated_json": dated_json,
        "dated_markdown": dated_markdown,
    }


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect read-only wiki + stonks paper-bot context for a personal Hermes Bloomberg brief")
    parser.add_argument("--wiki", type=Path, default=DEFAULT_WIKI_PATH, help="wiki path; default /home/matt/wiki")
    parser.add_argument("--bot-root", type=Path, default=DEFAULT_BOT_ROOT, help="stonks-paper-bot root")
    parser.add_argument("--config", type=Path, default=None, help="paper bot config path; default <bot-root>/config.paper.toml")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="where latest_context.{json,md} should be written")
    parser.add_argument("--quotes", action="store_true", help="also fetch public Stooq quotes for the first watchlist symbols")
    parser.add_argument("--quote-limit", type=int, default=25, help="max watchlist symbols to quote when --quotes is enabled")
    parser.add_argument("--news", action="store_true", help="also fetch public Google News RSS headlines for watchlist symbols")
    parser.add_argument("--news-symbol-limit", type=int, default=8, help="max watchlist symbols to include in the news RSS query")
    parser.add_argument("--no-service-check", action="store_true", help="skip systemctl user service status checks")
    parser.add_argument("--no-write", action="store_true", help="print only; do not write output files")
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    include_quotes = bool(args.quotes or env_truthy("HERMES_BLOOMBERG_QUOTES"))
    include_news = bool(args.news or env_truthy("HERMES_BLOOMBERG_NEWS"))
    snapshot = collect(
        wiki_path=args.wiki,
        bot_root=args.bot_root,
        config_path=args.config,
        include_quotes=include_quotes,
        quote_limit=max(1, args.quote_limit),
        include_news=include_news,
        news_symbol_limit=max(1, args.news_symbol_limit),
        check_services=not args.no_service_check,
    )
    if not args.no_write:
        write_outputs(snapshot, args.output_dir)
    if args.json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    else:
        print(render_markdown(snapshot), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
