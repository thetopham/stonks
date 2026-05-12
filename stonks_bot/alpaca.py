from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo
import json
import os
import shutil
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import BrokerConfig


class AlpacaConfigError(ValueError):
    pass


@dataclass(slots=True)
class AlpacaCredentials:
    endpoint: str
    key: str
    secret: str


@dataclass(slots=True)
class BrokerFill:
    order_id: str
    symbol: str
    side: str
    status: str
    quantity: float
    price: float
    notional: float


def alpaca_equity_24_5_is_open(now: datetime | None = None) -> bool:
    """Return true during Alpaca's Sunday 8 PM ET through Friday 8 PM ET equity window."""
    eastern = ZoneInfo("America/New_York")
    current = now or datetime.now(tz=eastern)
    if current.tzinfo is None:
        current = current.replace(tzinfo=eastern)
    else:
        current = current.astimezone(eastern)

    weekday = current.weekday()  # Monday=0, Sunday=6
    minutes = current.hour * 60 + current.minute
    sunday_open = 20 * 60
    friday_close = 20 * 60
    if weekday == 6:
        return minutes >= sunday_open
    if 0 <= weekday <= 3:
        return True
    if weekday == 4:
        return minutes < friday_close
    return False


def _format_order_decimal(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _parse_env_lines(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_dotenv(path: str | Path = ".env") -> bool:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return False
    for key, value in _parse_env_lines(env_path).items():
        if key not in os.environ or (os.environ.get(key) == "" and value):
            os.environ[key] = value
    return True


def load_env_files(paths: Iterable[str | Path]) -> list[Path]:
    loaded: list[Path] = []
    seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            resolved = path.resolve()
        except FileNotFoundError:
            resolved = path.absolute()
        if resolved in seen:
            continue
        seen.add(resolved)
        if load_dotenv(path):
            loaded.append(path)
    return loaded


def _hermes_cli_env_path() -> Path | None:
    hermes_bin = shutil.which("hermes")
    if not hermes_bin:
        return None
    try:
        result = subprocess.run(
            [hermes_bin, "config", "env-path"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not first_line:
        return None
    path = Path(first_line).expanduser()
    try:
        path_resolved = path.resolve()
        home_resolved = Path.home().resolve()
        if not path_resolved.is_relative_to(home_resolved):
            return None
    except (OSError, RuntimeError):
        return None
    return path


def default_env_paths(config_path: str | Path | None = None) -> list[Path]:
    paths: list[Path] = []
    if config_path is not None:
        paths.append(Path(config_path).expanduser().resolve().parent / ".env")
    hermes_env_path = _hermes_cli_env_path()
    if hermes_env_path is not None:
        paths.append(hermes_env_path)
    paths.extend(
        [
            Path.cwd() / ".env",
            Path.home() / ".hermes" / ".env",
            Path.home() / ".hermes" / "hermes-agent" / ".env",
            Path.home() / "hermes-workspace" / ".env",
        ]
    )
    return paths


def load_default_env_files(config_path: str | Path | None = None, extra_paths: Iterable[str | Path] | None = None) -> list[Path]:
    paths: list[str | Path] = []
    if extra_paths:
        paths.extend(extra_paths)
    paths.extend(default_env_paths(config_path))
    return load_env_files(paths)


def _env_value(name: str, *aliases: str) -> str | None:
    for candidate in (name, *aliases):
        if candidate in os.environ:
            value = os.environ.get(candidate)
            return value if value else None
    return None


def _aliases(name: str) -> tuple[str, ...]:
    return (name.upper(), name.lower())


def load_alpaca_credentials(broker: BrokerConfig) -> AlpacaCredentials:
    if broker.name not in {"alpaca", "none"}:
        raise AlpacaConfigError(f"unsupported broker '{broker.name}'")
    endpoint = _env_value(broker.endpoint_env, *_aliases(broker.endpoint_env), "APCA_API_BASE_URL") or "https://paper-api.alpaca.markets/v2"
    key = _env_value(broker.key_env, *_aliases(broker.key_env), "APCA_API_KEY_ID")
    secret = _env_value(broker.secret_env, *_aliases(broker.secret_env), "APCA_API_SECRET_KEY")

    missing = []
    if not key:
        missing.append(broker.key_env)
    if not secret:
        missing.append(broker.secret_env)
    if missing:
        raise AlpacaConfigError(f"missing Alpaca credential env value(s): {', '.join(missing)}")

    endpoint = endpoint.rstrip("/")
    if broker.paper_only and "paper-api.alpaca.markets" not in endpoint:
        raise AlpacaConfigError("Alpaca broker is paper_only=true but endpoint is not the paper endpoint")
    return AlpacaCredentials(endpoint=endpoint, key=key, secret=secret)


class AlpacaPaperClient:
    def __init__(
        self,
        credentials: AlpacaCredentials,
        timeout_seconds: int = 15,
        poll_attempts: int = 4,
        poll_delay_seconds: float = 1.0,
        data_endpoint: str = "https://data.alpaca.markets/v1beta1",
    ):
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds
        self.poll_attempts = poll_attempts
        self.poll_delay_seconds = poll_delay_seconds
        self.data_endpoint = data_endpoint.rstrip("/")

    def _request_json(self, path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        return self._request_json_url(f"{self.credentials.endpoint}{path}", path, method=method, payload=payload)

    def _request_json_url(self, url: str, label: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "APCA-API-KEY-ID": self.credentials.key,
            "APCA-API-SECRET-KEY": self.credentials.secret,
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise AlpacaConfigError(f"Alpaca request to {label} failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise AlpacaConfigError(f"Alpaca request to {label} failed: {exc.reason}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise AlpacaConfigError(f"Alpaca request to {label} returned invalid JSON") from exc

    def get_account(self) -> dict:
        return self._request_json("/account")

    def get_positions(self) -> list[dict]:
        positions = self._request_json("/positions")
        if not isinstance(positions, list):
            raise AlpacaConfigError("Alpaca request to /positions returned invalid JSON shape")
        return [position for position in positions if isinstance(position, dict)]

    def get_clock(self) -> dict:
        return self._request_json("/clock")

    def get_options_contracts(
        self,
        underlying_symbols: list[str],
        contract_type: str,
        expiration_date_gte: str,
        expiration_date_lte: str,
        strike_price_gte: float | None = None,
        strike_price_lte: float | None = None,
        limit: int = 1000,
        max_pages: int = 3,
    ) -> list[dict]:
        symbols = [symbol.upper() for symbol in underlying_symbols if symbol]
        if not symbols:
            return []
        if contract_type not in {"call", "put"}:
            raise AlpacaConfigError("option contract_type must be call or put")
        params: dict[str, str | int | float] = {
            "underlying_symbols": ",".join(symbols),
            "type": contract_type,
            "status": "active",
            "expiration_date_gte": expiration_date_gte,
            "expiration_date_lte": expiration_date_lte,
            "limit": max(1, min(int(limit), 10000)),
        }
        if strike_price_gte is not None:
            params["strike_price_gte"] = round(float(strike_price_gte), 2)
        if strike_price_lte is not None:
            params["strike_price_lte"] = round(float(strike_price_lte), 2)

        contracts: list[dict] = []
        page_token: str | None = None
        for _ in range(max(1, max_pages)):
            page_params = dict(params)
            if page_token:
                page_params["page_token"] = page_token
            path = "/options/contracts?" + urlencode(page_params)
            response = self._request_json(path)
            page_contracts = response.get("option_contracts", []) if isinstance(response, dict) else []
            if not isinstance(page_contracts, list):
                raise AlpacaConfigError("Alpaca options contracts endpoint returned invalid JSON shape")
            contracts.extend(contract for contract in page_contracts if isinstance(contract, dict))
            page_token = str(response.get("page_token") or "") if isinstance(response, dict) else ""
            if not page_token:
                break
        return contracts

    def get_latest_option_quotes(self, symbols: list[str], feed: str = "indicative") -> dict[str, dict]:
        clean_symbols = [symbol.upper() for symbol in symbols if symbol]
        if not clean_symbols:
            return {}
        if len(clean_symbols) > 100:
            raise AlpacaConfigError("Alpaca latest option quotes accepts at most 100 symbols per request")
        if feed not in {"indicative", "opra"}:
            raise AlpacaConfigError("option quote feed must be indicative or opra")
        params = urlencode({"symbols": ",".join(clean_symbols), "feed": feed})
        url = f"{self.data_endpoint}/options/quotes/latest?{params}"
        response = self._request_json_url(url, "/options/quotes/latest")
        quotes = response.get("quotes", {}) if isinstance(response, dict) else {}
        if not isinstance(quotes, dict):
            raise AlpacaConfigError("Alpaca latest option quotes endpoint returned invalid JSON shape")
        return {str(symbol).upper(): quote for symbol, quote in quotes.items() if isinstance(quote, dict)}

    def get_order(self, order_id: str) -> dict:
        return self._request_json(f"/orders/{order_id}")

    def cancel_order(self, order_id: str) -> dict:
        return self._request_json(f"/orders/{order_id}", method="DELETE")

    def market_is_open(self, *, extended_hours: bool = False) -> tuple[bool, str]:
        clock = self.get_clock()
        if bool(clock.get("is_open")):
            return True, "market open"
        next_open = clock.get("next_open") or "unknown"
        if extended_hours and alpaca_equity_24_5_is_open():
            return True, f"24/5 extended-hours session; next_regular_open={next_open}"
        return False, f"market closed; next_open={next_open}"

    def _submit_order_payload(self, payload: dict, *, fill_multiplier: float = 1.0) -> BrokerFill:
        order = self._request_json("/orders", method="POST", payload=payload)
        fill = self._filled_order(order, fill_multiplier=fill_multiplier)
        if fill is not None:
            return fill

        order_id = str(order.get("id") or "unknown")
        latest = order
        if order_id != "unknown":
            for _ in range(max(0, self.poll_attempts)):
                if self.poll_delay_seconds > 0:
                    time.sleep(self.poll_delay_seconds)
                latest = self.get_order(order_id)
                fill = self._filled_order(latest, fill_multiplier=fill_multiplier)
                if fill is not None:
                    return fill
                if str(latest.get("status", "")).lower() in {"canceled", "expired", "rejected"}:
                    break
            try:
                self.cancel_order(order_id)
            except AlpacaConfigError:
                pass
        status = str(latest.get("status", order.get("status", "unknown"))).lower()
        raise AlpacaConfigError(f"Alpaca paper order {order_id} status={status}; not filled after polling/cancel, so local ledger was not updated")

    def submit_market_order(self, symbol: str, *, side: str, notional: float | None = None, quantity: float | None = None) -> BrokerFill:
        side = side.lower()
        symbol = symbol.upper()
        if side not in {"buy", "sell"}:
            raise AlpacaConfigError("Alpaca order side must be buy or sell")
        if (notional is None) == (quantity is None):
            raise AlpacaConfigError("Alpaca market order requires exactly one of notional or quantity")
        if notional is not None and notional <= 0:
            raise AlpacaConfigError("Alpaca market order notional must be positive")
        if quantity is not None and quantity <= 0:
            raise AlpacaConfigError("Alpaca market order quantity must be positive")

        payload = {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }
        if notional is not None:
            payload["notional"] = f"{notional:.2f}"
        else:
            payload["qty"] = _format_order_decimal(quantity)
        return self._submit_order_payload(payload)

    def submit_extended_hours_limit_order(
        self,
        symbol: str,
        *,
        side: str,
        limit_price: float,
        notional: float | None = None,
        quantity: float | None = None,
        time_in_force: str = "day",
    ) -> BrokerFill:
        side = side.lower()
        symbol = symbol.upper()
        time_in_force = time_in_force.lower()
        if side not in {"buy", "sell"}:
            raise AlpacaConfigError("Alpaca extended-hours order side must be buy or sell")
        if time_in_force not in {"day", "gtc"}:
            raise AlpacaConfigError("Alpaca extended-hours order time_in_force must be day or gtc")
        if limit_price <= 0:
            raise AlpacaConfigError("Alpaca extended-hours limit_price must be positive")
        if (notional is None) == (quantity is None):
            raise AlpacaConfigError("Alpaca extended-hours limit order requires exactly one of notional or quantity")
        if notional is not None and notional <= 0:
            raise AlpacaConfigError("Alpaca extended-hours order notional must be positive")
        if quantity is not None and quantity <= 0:
            raise AlpacaConfigError("Alpaca extended-hours order quantity must be positive")

        payload = {
            "symbol": symbol,
            "side": side,
            "type": "limit",
            "limit_price": f"{limit_price:.2f}",
            "time_in_force": time_in_force,
            "extended_hours": True,
        }
        if notional is not None:
            payload["notional"] = f"{notional:.2f}"
        else:
            payload["qty"] = _format_order_decimal(quantity)
        return self._submit_order_payload(payload)

    def submit_crypto_market_order(
        self,
        symbol: str,
        *,
        side: str,
        notional: float | None = None,
        quantity: float | None = None,
    ) -> BrokerFill:
        side = side.lower()
        symbol = symbol.upper()
        if side not in {"buy", "sell"}:
            raise AlpacaConfigError("Alpaca crypto order side must be buy or sell")
        if (notional is None) == (quantity is None):
            raise AlpacaConfigError("Alpaca crypto market order requires exactly one of notional or quantity")
        if notional is not None and notional <= 0:
            raise AlpacaConfigError("Alpaca crypto market order notional must be positive")
        if quantity is not None and quantity <= 0:
            raise AlpacaConfigError("Alpaca crypto market order quantity must be positive")

        payload = {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "time_in_force": "gtc",
        }
        if notional is not None:
            payload["notional"] = f"{notional:.2f}"
        else:
            payload["qty"] = _format_order_decimal(quantity)
        return self._submit_order_payload(payload)

    def submit_option_limit_order(self, symbol: str, *, side: str, contracts: int, limit_price: float) -> BrokerFill:
        side = side.lower()
        symbol = symbol.upper()
        contracts = int(contracts)
        if side not in {"buy", "sell"}:
            raise AlpacaConfigError("Alpaca option order side must be buy or sell")
        if contracts <= 0:
            raise AlpacaConfigError("Alpaca option order contracts must be positive")
        if limit_price <= 0:
            raise AlpacaConfigError("Alpaca option order limit_price must be positive")
        payload = {
            "symbol": symbol,
            "qty": str(contracts),
            "side": side,
            "type": "limit",
            "limit_price": f"{limit_price:.2f}",
            "time_in_force": "day",
        }
        fill = self._submit_order_payload(payload, fill_multiplier=100.0)
        if int(round(fill.quantity)) != contracts:
            raise AlpacaConfigError(
                f"Alpaca option order {fill.order_id} filled unexpected contract quantity {fill.quantity}; local ledger was not updated"
            )
        return fill

    def submit_option_buy_to_open(self, symbol: str, *, contracts: int, limit_price: float) -> BrokerFill:
        return self.submit_option_limit_order(symbol, side="buy", contracts=contracts, limit_price=limit_price)

    def submit_option_sell_to_close(self, symbol: str, *, contracts: int, limit_price: float) -> BrokerFill:
        return self.submit_option_limit_order(symbol, side="sell", contracts=contracts, limit_price=limit_price)

    def submit_buy(self, symbol: str, notional: float) -> BrokerFill:
        return self.submit_market_order(symbol, side="buy", notional=notional)

    def submit_sell(self, symbol: str, quantity: float) -> BrokerFill:
        return self.submit_market_order(symbol, side="sell", quantity=quantity)

    def submit_extended_hours_buy(self, symbol: str, notional: float, limit_price: float, time_in_force: str = "day") -> BrokerFill:
        return self.submit_extended_hours_limit_order(symbol, side="buy", notional=notional, limit_price=limit_price, time_in_force=time_in_force)

    def submit_extended_hours_sell(self, symbol: str, quantity: float, limit_price: float, time_in_force: str = "day") -> BrokerFill:
        return self.submit_extended_hours_limit_order(symbol, side="sell", quantity=quantity, limit_price=limit_price, time_in_force=time_in_force)

    def submit_crypto_buy(self, symbol: str, notional: float) -> BrokerFill:
        return self.submit_crypto_market_order(symbol, side="buy", notional=notional)

    def submit_crypto_sell(self, symbol: str, quantity: float) -> BrokerFill:
        return self.submit_crypto_market_order(symbol, side="sell", quantity=quantity)

    def _filled_order(self, order: dict, *, fill_multiplier: float = 1.0) -> BrokerFill | None:
        status = str(order.get("status", "unknown")).lower()
        order_id = str(order.get("id") or "unknown")
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").lower()
        quantity = _as_float(order.get("filled_qty"), 0.0)
        price = _as_float(order.get("filled_avg_price"), 0.0)
        if status != "filled" or quantity <= 0 or price <= 0:
            return None
        return BrokerFill(order_id=order_id, symbol=symbol, side=side, status=status, quantity=quantity, price=price, notional=quantity * price * fill_multiplier)


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def format_account_summary(account: dict) -> str:
    fields = {
        "status": account.get("status", "unknown"),
        "equity": account.get("equity", "unknown"),
        "buying_power": account.get("buying_power", "unknown"),
        "options_buying_power": account.get("options_buying_power", "unknown"),
        "non_marginable_buying_power": account.get("non_marginable_buying_power", "unknown"),
        "trading_blocked": account.get("trading_blocked", "unknown"),
        "account_blocked": account.get("account_blocked", "unknown"),
        "pattern_day_trader": account.get("pattern_day_trader", "unknown"),
    }
    return ", ".join(f"{key}={value}" for key, value in fields.items())
