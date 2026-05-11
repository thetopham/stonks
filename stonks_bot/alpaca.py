from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import json
import os
import shutil
import subprocess
import time
from urllib.error import HTTPError, URLError
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
        if not value:
            continue
        if key not in os.environ or not os.environ.get(key):
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
    return Path(first_line).expanduser()


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
        value = os.environ.get(candidate)
        if value:
            return value
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
    def __init__(self, credentials: AlpacaCredentials, timeout_seconds: int = 15, poll_attempts: int = 4, poll_delay_seconds: float = 1.0):
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds
        self.poll_attempts = poll_attempts
        self.poll_delay_seconds = poll_delay_seconds

    def _request_json(self, path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "APCA-API-KEY-ID": self.credentials.key,
            "APCA-API-SECRET-KEY": self.credentials.secret,
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.credentials.endpoint}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise AlpacaConfigError(f"Alpaca request to {path} failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise AlpacaConfigError(f"Alpaca request to {path} failed: {exc.reason}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise AlpacaConfigError(f"Alpaca request to {path} returned invalid JSON") from exc

    def get_account(self) -> dict:
        return self._request_json("/account")

    def get_positions(self) -> list[dict]:
        positions = self._request_json("/positions")
        if not isinstance(positions, list):
            raise AlpacaConfigError("Alpaca request to /positions returned invalid JSON shape")
        return [position for position in positions if isinstance(position, dict)]

    def get_clock(self) -> dict:
        return self._request_json("/clock")

    def get_order(self, order_id: str) -> dict:
        return self._request_json(f"/orders/{order_id}")

    def cancel_order(self, order_id: str) -> dict:
        return self._request_json(f"/orders/{order_id}", method="DELETE")

    def market_is_open(self) -> tuple[bool, str]:
        clock = self.get_clock()
        if bool(clock.get("is_open")):
            return True, "market open"
        next_open = clock.get("next_open") or "unknown"
        return False, f"market closed; next_open={next_open}"

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
            payload["qty"] = f"{quantity:.8f}".rstrip("0").rstrip(".")

        order = self._request_json("/orders", method="POST", payload=payload)
        fill = self._filled_order(order)
        if fill is not None:
            return fill

        order_id = str(order.get("id") or "unknown")
        latest = order
        if order_id != "unknown":
            for _ in range(max(0, self.poll_attempts)):
                if self.poll_delay_seconds > 0:
                    time.sleep(self.poll_delay_seconds)
                latest = self.get_order(order_id)
                fill = self._filled_order(latest)
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

    def submit_buy(self, symbol: str, notional: float) -> BrokerFill:
        return self.submit_market_order(symbol, side="buy", notional=notional)

    def submit_sell(self, symbol: str, quantity: float) -> BrokerFill:
        return self.submit_market_order(symbol, side="sell", quantity=quantity)

    def _filled_order(self, order: dict) -> BrokerFill | None:
        status = str(order.get("status", "unknown")).lower()
        order_id = str(order.get("id") or "unknown")
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").lower()
        quantity = _as_float(order.get("filled_qty"), 0.0)
        price = _as_float(order.get("filled_avg_price"), 0.0)
        if status != "filled" or quantity <= 0 or price <= 0:
            return None
        return BrokerFill(order_id=order_id, symbol=symbol, side=side, status=status, quantity=quantity, price=price, notional=quantity * price)


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
        "trading_blocked": account.get("trading_blocked", "unknown"),
        "account_blocked": account.get("account_blocked", "unknown"),
        "pattern_day_trader": account.get("pattern_day_trader", "unknown"),
    }
    return ", ".join(f"{key}={value}" for key, value in fields.items())
