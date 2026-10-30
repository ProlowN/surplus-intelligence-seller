"""Surplus Intelligence seller HTTP client, Venice only.

Patterns follow the OpenClaw seller plugin (SIWE mint, Idempotency-Key on
creates, cost_multiplier pricing, NDJSON discover/health) without buyer flows
or any provider other than Venice.
"""

from __future__ import annotations

import json
import threading
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from . import strategy

API_ORIGIN = "https://api.surplusintelligence.ai"
VENICE_BASE_URL = "https://api.venice.ai/api/v1"
USER_AGENT = "surplus-intelligence-seller/0.1.0 (hermes-plugin)"
PLUGIN_LABEL = "hermes-surplus-seller"

EARNINGS_RANGES = ("today", "7d", "30d", "90d", "mtd", "lifetime")
OFFER_STATUSES = ("active", "paused", "inactive")
# Surplus persists a pause as inactive. Filtering on the literal "paused"
# value does not match those rows.
STORED_STATUS = {"paused": "inactive"}

_SECRET_NAMES = (
    "SURPLUS_SELLER_API_KEY",
    "VENICE_API_KEY",
    "VENICE_ADMIN_API_KEY",
    "SURPLUS_SELLER_PAYOUT_ADDRESS",
)
_SENSITIVE_RESPONSE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "private_key",
    "privatekey",
    "signature",
    "secret",
}


class SurplusError(Exception):
    """Seller API or input error safe to return to the model."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.detail = detail

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error": self.message}
        if self.status is not None:
            payload["status"] = self.status
        if self.code:
            payload["code"] = self.code
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


def read_secret(name: str, *, lookup: Callable[[str], Any] | None = None) -> str | None:
    """Read one Hermes secret. Never opens a dotenv file.

    ``lookup`` replaces ``agent.secret_scope.get_secret`` in tests. Inside
    Hermes, ``get_secret`` is the only credential route (catalog rule 11).
    """
    if name not in _SECRET_NAMES:
        raise SurplusError(f"Refusing to read undeclared secret name {name}.")
    getter = lookup
    if getter is None:
        try:
            from agent.secret_scope import get_secret as getter  # type: ignore
        except Exception as exc:  # ImportError and hermes init failures
            raise SurplusError(
                "Hermes secret scope is unavailable, so this plugin cannot read "
                f"{name}. Set it with Hermes (requires_env / hermes secrets) and retry."
            ) from exc
    try:
        value = getter(name)
    except Exception as exc:
        raise SurplusError(f"Could not read {name} from Hermes secrets: {type(exc).__name__}") from exc
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def micro_usdc_to_usd(value: Any) -> str | None:
    """Surplus ``*_usdc`` fields are integer micro-USD strings (1 USDC = 1_000_000)."""
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value)) / Decimal(1_000_000)
    except (InvalidOperation, ValueError):
        return None
    quantized = amount.quantize(Decimal("0.000001"))
    return format(quantized, "f")


def _redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def _json_ready(value: Any, secrets: list[str]) -> Any:
    """Redact known secret values without dropping response fields.

    Auth issuance returns the new seller key in ``key`` (or ``api_key``). That
    value is not yet a known secret, so it survives once for the caller to save.
    Echoes of the Venice key or an existing seller key are replaced.
    """
    if isinstance(value, str):
        return _redact(value, secrets)
    if isinstance(value, dict):
        return {str(key): _json_ready(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item, secrets) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact(str(value), secrets)


def _strip_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _strip_sensitive(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_RESPONSE_KEYS
        }
    if isinstance(value, list):
        return [_strip_sensitive(item) for item in value]
    return value


def parse_pricing(*, cost_multiplier: Any = None, discount_pct: Any = None) -> dict[str, Any]:
    """Build a cost_multiplier body. Per-token prices are retired for text."""
    if cost_multiplier is None and discount_pct is None:
        raise SurplusError(
            f"Provide cost_multiplier ({_MIN_MULT}–{_MAX_MULT}) or discount_pct (0–{_MAX_DISC}). "
            "Per-token pricing is retired and is not sent."
        )

    discount = _parse_discount(discount_pct) if discount_pct is not None else None
    multiplier = _parse_multiplier(cost_multiplier) if cost_multiplier is not None else None

    if discount is not None and multiplier is None:
        multiplier = float(Decimal(1) - (Decimal(str(discount)) / Decimal(100)))
        multiplier = float(Decimal(str(multiplier)).quantize(Decimal("0.000001")))
    elif multiplier is not None and discount is None:
        implied = (Decimal(1) - Decimal(str(multiplier))) * Decimal(100)
        if Decimal(0) <= implied <= Decimal(100):
            discount = float(implied.quantize(Decimal("0.000001")))
    elif discount is not None and multiplier is not None:
        expected = Decimal(1) - (Decimal(str(discount)) / Decimal(100))
        if abs(expected - Decimal(str(multiplier))) > Decimal("0.001"):
            raise SurplusError(
                "cost_multiplier and discount_pct disagree. "
                "discount_pct 15 means cost_multiplier 0.85."
            )

    if multiplier is None:
        raise SurplusError(_MULTIPLIER_RANGE_ERROR)
    _check_multiplier_bounds(multiplier)

    body: dict[str, Any] = {
        "pricing_mode": "cost_multiplier",
        "cost_multiplier": multiplier,
    }
    # Text models resolve the multiplier; media models read discount. Send both
    # when the discount is in range so one body covers either billing shape.
    if discount is not None and 0 <= discount <= 100:
        body["discount"] = discount
    return body


_MIN_MULT = format(strategy.MIN_COST_MULTIPLIER, "g")
_MAX_MULT = format(strategy.MAX_COST_MULTIPLIER, "g")
_MAX_DISC = format(strategy.MAX_DISCOUNT_PCT, "g")
_MULTIPLIER_RANGE_ERROR = (
    f"cost_multiplier must be between {_MIN_MULT} and {_MAX_MULT} inclusive "
    f"(discount_pct at most {_MAX_DISC})."
)


def _check_multiplier_bounds(number: float) -> None:
    if number < strategy.MIN_COST_MULTIPLIER - 1e-9:
        raise SurplusError(
            f"cost_multiplier {format(number, 'g')} is below the floor of {_MIN_MULT} "
            f"({_MAX_DISC}% off). The plugin will not list Venice capacity for free or "
            f"near-free. Use {_MIN_MULT} or higher; the automatic clearance price is "
            f"{format(1 - strategy.CLEARANCE_DISCOUNT_PCT / 100, 'g')}.",
            code="cost_multiplier_below_floor",
        )
    if number > strategy.MAX_COST_MULTIPLIER + 1e-9:
        raise SurplusError(_MULTIPLIER_RANGE_ERROR)


def _parse_multiplier(value: Any) -> float:
    number = _finite_number(value, "cost_multiplier")
    _check_multiplier_bounds(number)
    return number


def _parse_discount(value: Any) -> float:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.endswith("%"):
            stripped = stripped[:-1].strip()
        value = stripped
    number = _finite_number(value, "discount_pct")
    if number < 0:
        raise SurplusError(f"discount_pct must be between 0 and {_MAX_DISC} inclusive.")
    if number > strategy.MAX_DISCOUNT_PCT + 1e-9:
        raise SurplusError(
            f"discount_pct {format(number, 'g')} is above the maximum of {_MAX_DISC} "
            f"(cost_multiplier floor {_MIN_MULT}). The plugin will not list Venice "
            "capacity for free or near-free.",
            code="cost_multiplier_below_floor",
        )
    return number


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or value is None:
        raise SurplusError(f"{label} must be a finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SurplusError(f"{label} must be a finite number.") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise SurplusError(f"{label} must be a finite number.")
    return number


def _positive_usd(value: Any, label: str) -> float:
    number = _finite_number(value, label)
    if number <= 0:
        raise SurplusError(f"{label} must be greater than 0.")
    return number


def _checksum_address(value: str, label: str) -> str:
    text = value.strip()
    if len(text) != 42 or not text.startswith("0x"):
        raise SurplusError(f"{label} must be a 0x-prefixed 20-byte hex address.")
    hex_part = text[2:]
    if any(ch not in "0123456789abcdefABCDEF" for ch in hex_part):
        raise SurplusError(f"{label} must be a 0x-prefixed 20-byte hex address.")
    return text


def _offer_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SurplusError("offer_id is required.")
    offer_id = value.strip()
    if len(offer_id) > 128 or any(ch in offer_id for ch in "/\\?#"):
        raise SurplusError("offer_id contains unsupported characters.")
    return offer_id


def _model_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SurplusError("model is required.")
    model = value.strip()
    if len(model) > 200 or any(ch.isspace() for ch in model):
        raise SurplusError("model must be a single Venice model id without spaces.")
    return model


def default_wallet_factory() -> tuple[str, Callable[[str], str]]:
    """Create a throwaway signing wallet.

    The private key exists only inside the returned ``sign`` closure. Callers
    must drop that closure after one signature. It is never logged or stored.
    """
    from eth_account import Account
    from eth_account.messages import encode_defunct

    account = Account.create()
    address = account.address

    def sign(message: str) -> str:
        signed = account.sign_message(encode_defunct(text=message))
        raw = signed.signature.hex()
        if raw.startswith(("0x", "0X")):
            raw = raw[2:]
        return "0x" + raw

    return address, sign


class SurplusClient:
    def __init__(
        self,
        *,
        transport: Callable[..., tuple[int, dict[str, str], bytes]] | None = None,
        secret_lookup: Callable[[str], Any] | None = None,
        wallet_factory: Callable[[], tuple[str, Callable[[str], str]]] | None = None,
        origin: str = API_ORIGIN,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = transport or urllib_transport
        self._secret_lookup = secret_lookup
        self._wallet_factory = wallet_factory or default_wallet_factory
        self._origin = origin.rstrip("/")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._session_seller_key: str | None = None
        self._lock = threading.Lock()

    def secret(self, name: str) -> str | None:
        if name == "SURPLUS_SELLER_API_KEY":
            configured = read_secret(name, lookup=self._secret_lookup)
            if configured:
                return configured
            return self._session_seller_key
        return read_secret(name, lookup=self._secret_lookup)

    def seller_key_source(self) -> str | None:
        configured = read_secret("SURPLUS_SELLER_API_KEY", lookup=self._secret_lookup)
        if configured:
            return "hermes_secret"
        if self._session_seller_key:
            return "session"
        return None

    def require_seller_key(self) -> str:
        key = self.secret("SURPLUS_SELLER_API_KEY")
        if not key:
            raise SurplusError(
                "No Surplus seller key. Call surplus_seller_auth to mint an "
                "si_seller_ key, then save it as SURPLUS_SELLER_API_KEY."
            )
        if not key.startswith("si_seller_"):
            raise SurplusError("SURPLUS_SELLER_API_KEY must be an si_seller_ key.")
        return key

    def require_venice_key(self) -> str:
        key = self.secret("VENICE_API_KEY")
        if not key:
            raise SurplusError(
                "VENICE_API_KEY is not set. This plugin only resells a Venice key "
                f"against {VENICE_BASE_URL}."
            )
        return key

    def require_payout(self) -> str:
        address = self.secret("SURPLUS_SELLER_PAYOUT_ADDRESS")
        if not address:
            raise SurplusError(
                "SURPLUS_SELLER_PAYOUT_ADDRESS is not set. Earnings settle in USDC "
                "on Base to this wallet."
            )
        return _checksum_address(address, "SURPLUS_SELLER_PAYOUT_ADDRESS")

    def known_secrets(self) -> list[str]:
        values = []
        for name in ("SURPLUS_SELLER_API_KEY", "VENICE_API_KEY", "VENICE_ADMIN_API_KEY"):
            try:
                value = self.secret(name)
            except SurplusError:
                value = None
            if value:
                values.append(value)
        return values

    def mint_seller_key(self, *, label: str | None = None, issue_additional: bool = False) -> dict[str, Any]:
        """Mint a seller key, or issue another key on the current seller account.

        A fresh SIWE mint uses a new throwaway wallet and therefore a new seller
        identity. It runs only when no seller key is configured. Additional keys
        use ``POST /v1/seller/keys`` so they stay on the same account.
        """
        safe_label = _label(label)
        source = self.seller_key_source()
        if source and not issue_additional:
            key = self.require_seller_key()
            return {
                "ok": True,
                "already_configured": True,
                "source": source,
                "key_prefix": _prefix(key),
                "private_key_stored": False,
                "message": (
                    "A seller key is already available. A new SIWE mint would create "
                    "a different seller identity with none of the current offers. "
                    "Pass issue_additional=true to mint another si_seller_ key on "
                    "this same account, or unset SURPLUS_SELLER_API_KEY before minting "
                    "a new identity."
                ),
            }
        if issue_additional:
            if not source:
                raise SurplusError(
                    "issue_additional requires an existing seller key. "
                    "Call surplus_seller_auth without it to mint the first key."
                )
            issued = self._issue_additional_key(safe_label)
            return _issued_key_payload(issued, source="additional_key", wallet=issued.get("wallet"))

        address, sign = self._wallet_factory()
        try:
            challenge = self.request(
                "GET",
                "/v1/seller/auth/challenge",
                query={"address": address},
                auth=False,
            )
            message = challenge.get("message") if isinstance(challenge, dict) else None
            if not isinstance(message, str) or not message.strip():
                raise SurplusError("Seller auth challenge returned no SIWE message.")
            try:
                signature = sign(message)
            except Exception as exc:
                raise SurplusError(
                    f"SIWE signing failed ({type(exc).__name__}). The throwaway wallet was discarded."
                ) from None
        finally:
            sign = None  # noqa: F841 — drop the closure that holds the private key

        issued = self.request(
            "POST",
            "/v1/seller/auth/keys",
            json_body={"message": message, "signature": signature, "label": safe_label},
            auth=False,
            idempotent=True,
        )
        raw_key = ""
        if isinstance(issued, dict):
            raw_key = str(issued.get("key") or issued.get("api_key") or "")
        if not raw_key.startswith("si_seller_"):
            raise SurplusError("Seller key issuance returned no si_seller_ key.")
        with self._lock:
            # Keep the issued key for this process only. Hermes restart forgets it.
            if self.seller_key_source() != "hermes_secret":
                self._session_seller_key = raw_key
        return _issued_key_payload(issued, source="siwe_mint", wallet=address, key=raw_key)

    def _issue_additional_key(self, label: str) -> dict[str, Any]:
        issued = self.request(
            "POST",
            "/v1/seller/keys",
            json_body={"label": label},
            idempotent=True,
        )
        if not isinstance(issued, dict):
            raise SurplusError("Additional seller key issuance returned no JSON body.")
        return issued

    def list_offers(self, *, status: str | None = None, model: str | None = None) -> dict[str, Any]:
        stored_status = None
        note = None
        if status:
            if status not in OFFER_STATUSES:
                raise SurplusError("status must be active, paused, or inactive.")
            stored_status = STORED_STATUS.get(status, status)
            if status == "paused":
                note = (
                    "Surplus stores a pause as inactive. This list is the inactive "
                    "set, which includes paused and cancelled offers."
                )
        if model:
            model = _model_name(model)
        items: list[dict[str, Any]] = []
        next_token = None
        for _page in range(10):
            query: dict[str, str] = {}
            if stored_status:
                query["status"] = stored_status
            if model:
                query["model"] = model
            if next_token:
                query["next_token"] = next_token
            data = self.request("GET", "/v1/seller/offers", query=query)
            batch = []
            if isinstance(data, dict):
                raw_items = data.get("items") or data.get("offers") or []
                if isinstance(raw_items, list):
                    batch = raw_items
                next_token = data.get("next_token") or None
            items.extend(_public_offer(item) for item in batch if isinstance(item, dict))
            if not next_token:
                break
        payload: dict[str, Any] = {
            "ok": True,
            "provider": "venice",
            "count": len(items),
            "offers": items,
        }
        if note:
            payload["status_note"] = note
        if next_token:
            payload["truncated"] = True
        return payload

    def create_offer(
        self,
        *,
        model: str,
        cost_multiplier: Any = None,
        discount_pct: Any = None,
        cap_daily_usd: Any = None,
    ) -> dict[str, Any]:
        strategy_notes: list[str] = []
        recommendation = None
        capacity = None
        need_price = cost_multiplier is None and discount_pct is None
        need_cap = cap_daily_usd is None
        if need_price or need_cap:
            try:
                capacity = self.read_venice_diem()
                recommendation = capacity.get("recommendation")
            except SurplusError as exc:
                strategy_notes.append(exc.message)
        if need_price:
            if isinstance(recommendation, dict) and recommendation.get("discount_pct") is not None:
                discount_pct = recommendation["discount_pct"]
                strategy_notes.append(
                    "Price omitted. Using the utilization-first discount so today's DIEM is more likely to clear."
                )
            else:
                discount_pct = strategy.OPENING_DISCOUNT_PCT
                strategy_notes.append(
                    "DIEM balance was not readable. Using the opening utilization price, "
                    f"{strategy.OPENING_DISCOUNT_PCT:.0f}% off reference, instead of a high-margin price."
                )
        if need_cap:
            suggested = capacity.get("suggested_cap_daily_usd") if isinstance(capacity, dict) else None
            if suggested:
                cap_daily_usd = suggested
                strategy_notes.append(
                    "cap_daily_usd set to the DIEM epoch budget so Surplus can route the full day's credit. "
                    "A low arbitrary cap would leave DIEM unused at 00:00 UTC."
                )
            else:
                strategy_notes.append(
                    "No DIEM allocation was readable, so no daily cap was set. "
                    "An omitted cap does not block the full allowance. Do not substitute a small cap."
                )
        pricing = parse_pricing(cost_multiplier=cost_multiplier, discount_pct=discount_pct)
        body: dict[str, Any] = {
            "model": _model_name(model),
            "api_key": self.require_venice_key(),
            "seller_base_url": VENICE_BASE_URL,
            "payout_address": self.require_payout(),
            **pricing,
        }
        if cap_daily_usd is not None:
            body["cap_daily_usd"] = _positive_usd(cap_daily_usd, "cap_daily_usd")
            remaining = capacity.get("diem_remaining_usd") if isinstance(capacity, dict) else None
            if isinstance(remaining, (int, float)) and body["cap_daily_usd"] + 0.009 < remaining:
                strategy_notes.append(
                    "This cap is below DIEM still remaining. Surplus may stop routing while allowance expires."
                )
        data = self.request("POST", "/v1/seller/offers", json_body=body, idempotent=True)
        offer = _public_offer(data) if isinstance(data, dict) else {"raw": data}
        payload: dict[str, Any] = {
            "ok": True,
            "goal": strategy.GOAL,
            "provider": "venice",
            "seller_base_url": VENICE_BASE_URL,
            "offer": offer,
            "pricing": {k: v for k, v in pricing.items()},
        }
        if recommendation:
            payload["recommendation"] = recommendation
        if strategy_notes:
            payload["notes"] = strategy_notes
        return payload

    def update_offer(
        self,
        *,
        offer_id: str,
        cost_multiplier: Any = None,
        discount_pct: Any = None,
        cap_daily_usd: Any = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        if cost_multiplier is None and discount_pct is None and cap_daily_usd is None and not status:
            raise SurplusError(
                "Provide cost_multiplier, discount_pct, cap_daily_usd, or status to update."
            )
        body: dict[str, Any] = {}
        if cost_multiplier is not None or discount_pct is not None:
            body.update(parse_pricing(cost_multiplier=cost_multiplier, discount_pct=discount_pct))
        if cap_daily_usd is not None:
            body["cap_daily_usd"] = _positive_usd(cap_daily_usd, "cap_daily_usd")
        if status:
            normalized = status.strip().lower()
            if normalized not in {"active", "paused"}:
                raise SurplusError("status must be active or paused.")
            body["status"] = normalized
        data = self.request(
            "PATCH",
            f"/v1/seller/offers/{urllib.parse.quote(_offer_id(offer_id), safe='')}",
            json_body=body,
        )
        return {
            "ok": True,
            "offer": _public_offer(data) if isinstance(data, dict) else {"id": offer_id},
            "goal": strategy.GOAL,
        }

    def pause_or_cancel(self, *, offer_id: str, action: str) -> dict[str, Any]:
        normalized = (action or "").strip().lower()
        target = _offer_id(offer_id)
        path = f"/v1/seller/offers/{urllib.parse.quote(target, safe='')}"
        if normalized == "pause":
            data = self.request("PATCH", path, json_body={"status": "paused"})
            offer = _public_offer(data) if isinstance(data, dict) else {}
            return {
                "ok": True,
                "action": "pause",
                "offer_id": target,
                "offer": offer,
                "note": "Pause is stored as inactive and stops routing. surplus_resume_offer relists it.",
            }
        if normalized in {"cancel", "delete", "deactivate"}:
            self.request("DELETE", path)
            return {
                "ok": True,
                "action": "cancel",
                "offer_id": target,
                "note": (
                    "Offer soft-deleted (removed from routing; Surplus retains the encrypted "
                    "Venice key). Revoke the Venice key if you need immediate invalidation. "
                    "surplus_resume_offer PATCHes status back to active when the slot is free."
                ),
            }
        raise SurplusError("action must be pause or cancel. Use surplus_resume_offer to relist.")

    def resume_offer(self, *, offer_id: str) -> dict[str, Any]:
        target = _offer_id(offer_id)
        data = self.request(
            "PATCH",
            f"/v1/seller/offers/{urllib.parse.quote(target, safe='')}",
            json_body={"status": "active"},
        )
        return {
            "ok": True,
            "action": "resume",
            "offer_id": target,
            "offer": _public_offer(data) if isinstance(data, dict) else {},
            "note": (
                "Reactivating an offer re-checks the one-active-offer rule for "
                "(Venice key, https://api.venice.ai/api/v1, model). "
                "A duplicate returns 409 duplicate_offer_for_credential. "
                "An operator-delisted offer returns 400 offer_delisted_by_operator."
            ),
        }

    def earnings(self, *, range_name: str = "7d") -> dict[str, Any]:
        if range_name not in EARNINGS_RANGES:
            raise SurplusError(
                "range must be one of: " + ", ".join(EARNINGS_RANGES) + "."
            )
        earnings = self.request("GET", "/v1/seller/earnings", query={"range": range_name})
        payouts = self.request("GET", "/v1/seller/payouts")
        return {
            "ok": True,
            "range": range_name,
            "earnings": _summarize_earnings(earnings if isinstance(earnings, dict) else {}),
            "payouts": _summarize_payouts(payouts if isinstance(payouts, dict) else {}),
            "note": (
                "Earnings accrue per response. Payouts batch in USDC on Base "
                "(read threshold_usdc; historically 5 USDC or 72 hours). "
                "A per-request settlement_status of accrued does not mean unpaid. "
                "Confirm payment from payout history entries with status paid and a Base tx_hash. "
                "This tool does not withdraw or redirect payouts."
            ),
        }

    def discover_models(self, *, limit: int = 80) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise SurplusError("limit must be an integer from 1 to 200.")
        lines = self.request_ndjson(
            "POST",
            "/v1/seller/discover",
            json_body={"api_key": self.require_venice_key(), "base_url": VENICE_BASE_URL},
            timeout=120,
        )
        supported = [row for row in lines if isinstance(row, dict) and row.get("supported") is not False]
        # Some payloads omit `supported` and only list sellable models.
        if supported and all(row.get("supported") is None for row in supported):
            supported = [row for row in lines if isinstance(row, dict) and row.get("model")]
        view = [_public_model(row) for row in supported[:limit]]
        return {
            "ok": True,
            "provider": "venice",
            "base_url": VENICE_BASE_URL,
            "discovered": len(lines),
            "supported": len(supported),
            "returned": len(view),
            "truncated": len(supported) > len(view),
            "models": view,
        }

    def test_connection(self, *, models: list[str]) -> dict[str, Any]:
        names = [_model_name(model) for model in models]
        if not names:
            raise SurplusError("Provide at least one Venice model id to probe.")
        body = {
            "api_key": self.require_venice_key(),
            "base_url": VENICE_BASE_URL,
        }
        if len(names) == 1:
            body["model"] = names[0]
            data = self.request(
                "POST",
                "/v1/seller/test-connection",
                json_body=body,
                timeout=90,
            )
            return _probe_payload(names, data if isinstance(data, dict) else {"raw": data})
        body["models"] = names
        lines = self.request_ndjson(
            "POST",
            "/v1/seller/test-health",
            json_body=body,
            timeout=120,
        )
        return {
            "ok": all(isinstance(row, dict) and row.get("ok") for row in lines) and bool(lines),
            "provider": "venice",
            "base_url": VENICE_BASE_URL,
            "results": [_json_ready(row, self.known_secrets()) for row in lines],
        }

    def read_venice_diem(self) -> dict[str, Any]:
        """Remaining DIEM, epoch allocation when Venice returns it, and the fill price."""
        now = self._clock()
        venice_key = self.require_venice_key()
        admin_key = None
        try:
            admin_key = self.secret("VENICE_ADMIN_API_KEY")
        except SurplusError:
            admin_key = None
        billing = None
        billing_error = None
        billing_source = None
        attempts: list[tuple[str, str]] = []
        if admin_key:
            attempts.append(("VENICE_ADMIN_API_KEY", admin_key))
        if not admin_key or admin_key != venice_key:
            attempts.append(("VENICE_API_KEY", venice_key))
        for label, key in attempts:
            try:
                billing = self.request(
                    "GET",
                    "/billing/balance",
                    auth=False,
                    bearer=key,
                    base=VENICE_BASE_URL,
                )
                billing_source = label
                billing_error = None
                break
            except SurplusError as exc:
                billing = None
                billing_error = exc.message
                if exc.status not in (401, 403):
                    break
        rate = None
        rate_error = None
        try:
            rate = self.request(
                "GET",
                "/api_keys/rate_limits",
                auth=False,
                bearer=venice_key,
                base=VENICE_BASE_URL,
            )
        except SurplusError as exc:
            rate_error = exc.message
        snapshot = strategy.interpret_venice(
            billing if isinstance(billing, dict) else None,
            rate if isinstance(rate, dict) else None,
            now,
            billing_error=billing_error,
            rate_error=rate_error,
            billing_source=billing_source,
        )
        if snapshot.get("diem_remaining_usd") is None and not snapshot.get("sources"):
            detail = billing_error or rate_error or "Venice returned no DIEM balance."
            raise SurplusError(f"Could not read Venice DIEM capacity. {detail}")
        return snapshot

    def bulk_create_offers(self, offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create many Venice offers, then point each payout at the configured wallet.

        The bulk endpoint has no payout field. A mint from a throwaway SIWE wallet
        would otherwise settle USDC to a key that was discarded, so a payout PATCH
        that fails cancels the new offer.
        """
        if not offers:
            return []
        payout = self.require_payout()
        venice_key = self.require_venice_key()
        created: list[dict[str, Any]] = []
        for start in range(0, len(offers), 20):
            chunk = offers[start : start + 20]
            body = {
                "api_key": venice_key,
                "base_url": VENICE_BASE_URL,
                "offers": chunk,
            }
            try:
                lines = self.request_ndjson("POST", "/v1/seller/offers/bulk", json_body=body, timeout=120)
            except SurplusError:
                lines = []
                for spec in chunk:
                    try:
                        single = self.create_offer(
                            model=spec["model"],
                            cost_multiplier=spec.get("cost_multiplier"),
                            discount_pct=spec.get("discount_pct", spec.get("discount")),
                            cap_daily_usd=spec.get("cap_daily_usd"),
                        )
                        offer = single.get("offer") if isinstance(single.get("offer"), dict) else {}
                        created.append(
                            {
                                "ok": True,
                                "model": spec["model"],
                                "offer_id": offer.get("id") or offer.get("offer_id"),
                                "via": "single",
                            }
                        )
                    except SurplusError as exc:
                        created.append({"ok": False, "model": spec.get("model"), "error": exc.message})
                continue
            if not lines:
                for spec in chunk:
                    created.append({"ok": False, "model": spec.get("model"), "error": "Bulk create returned no rows."})
                continue
            for line in lines:
                if not isinstance(line, dict):
                    continue
                model = line.get("model")
                offer_id = line.get("offer_id") or line.get("id")
                status = str(line.get("status") or "")
                if status not in {"created", "ok"} and not offer_id:
                    created.append(
                        {
                            "ok": False,
                            "model": model,
                            "error": line.get("error") or line.get("message") or status or "not created",
                        }
                    )
                    continue
                if not offer_id:
                    created.append({"ok": False, "model": model, "error": "Bulk create did not return an offer id."})
                    continue
                try:
                    self.request(
                        "PATCH",
                        f"/v1/seller/offers/{urllib.parse.quote(str(offer_id), safe='')}",
                        json_body={"payout_address": payout},
                    )
                    created.append({"ok": True, "model": model, "offer_id": offer_id, "payout_set": True})
                except SurplusError as exc:
                    cancelled = False
                    try:
                        self.request(
                            "DELETE",
                            f"/v1/seller/offers/{urllib.parse.quote(str(offer_id), safe='')}",
                        )
                        cancelled = True
                    except SurplusError:
                        cancelled = False
                    created.append(
                        {
                            "ok": False,
                            "model": model,
                            "offer_id": offer_id,
                            "error": exc.message,
                            "cancelled": cancelled,
                            "reason": (
                                "Payout address could not be set. The offer was cancelled so earnings "
                                "would not settle to the discarded SIWE wallet."
                                if cancelled
                                else "Payout address could not be set and the offer could not be cancelled. Cancel it manually."
                            ),
                        }
                    )
        return created

    def status_report(self) -> dict[str, Any]:
        seller_source = None
        seller_prefix = None
        try:
            seller_source = self.seller_key_source()
            if seller_source:
                seller_prefix = _prefix(self.require_seller_key())
        except SurplusError as exc:
            seller_source = f"error: {exc.message}"
        venice_set = False
        payout_preview = None
        try:
            venice_set = bool(self.secret("VENICE_API_KEY"))
        except SurplusError:
            venice_set = False
        try:
            payout = self.secret("SURPLUS_SELLER_PAYOUT_ADDRESS")
            if payout:
                payout_preview = payout[:6] + "…" + payout[-4:]
        except SurplusError:
            payout_preview = None
        report: dict[str, Any] = {
            "plugin": "surplus-intelligence-seller",
            "provider": "venice",
            "venice_base_url": VENICE_BASE_URL,
            "api_origin": self._origin,
            "seller_key": seller_source or "missing",
            "seller_key_prefix": seller_prefix,
            "venice_key": "configured" if venice_set else "missing",
            "payout_address": payout_preview or "missing",
            "private_key_stored": False,
            "goal": strategy.GOAL,
        }
        try:
            diem = self.read_venice_diem()
            report["diem_remaining_usd"] = diem.get("diem_remaining_usd")
            report["diem_epoch_allocation_usd"] = diem.get("diem_epoch_allocation_usd")
            report["diem_consumed_usd"] = diem.get("diem_consumed_usd")
            report["hours_left"] = diem.get("hours_left")
            report["leftover_diem_risk"] = diem.get("leftover_diem_risk")
            report["suggested_cap_daily_usd"] = diem.get("suggested_cap_daily_usd")
        except SurplusError as exc:
            report["diem_error"] = exc.message
        if seller_source in {"hermes_secret", "session"}:
            try:
                offers = self.list_offers()
                report["offer_count"] = offers.get("count", 0)
                report["offers"] = [
                    {
                        "id": item.get("id") or item.get("offer_id"),
                        "model": item.get("model"),
                        "status": item.get("status"),
                        "healthy": item.get("healthy"),
                        "cost_multiplier": item.get("cost_multiplier"),
                    }
                    for item in offers.get("offers", [])[:20]
                ]
            except SurplusError as exc:
                report["offers_error"] = exc.message
        return report

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
        idempotent: bool = False,
        timeout: float = 30,
        base: str | None = None,
        bearer: str | None = None,
    ) -> Any:
        status, _headers, payload = self._raw(
            method,
            path,
            query=query,
            json_body=json_body,
            auth=auth,
            idempotent=idempotent,
            timeout=timeout,
            base=base,
            bearer=bearer,
        )
        if status == 204 or payload == b"":
            return {"ok": True, "status": status}
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SurplusError(
                f"Surplus returned a non-JSON body (HTTP {status}).",
                status=status,
            ) from exc
        return _json_ready(decoded, self._body_secrets(json_body))

    def request_ndjson(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float = 120,
    ) -> list[Any]:
        status, _headers, payload = self._raw(
            method,
            path,
            json_body=json_body,
            auth=True,
            idempotent=True,
            timeout=timeout,
        )
        if status == 204 or payload == b"":
            return []
        text = payload.decode("utf-8", errors="replace")
        secrets = self._body_secrets(json_body)
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        rows: list[Any]
        if isinstance(decoded, list):
            rows = decoded
        elif isinstance(decoded, dict) and isinstance(decoded.get("models"), list):
            rows = decoded["models"]
        elif isinstance(decoded, dict) and isinstance(decoded.get("items"), list):
            rows = decoded["items"]
        elif isinstance(decoded, dict) and isinstance(decoded.get("results"), list):
            rows = decoded["results"]
        else:
            rows = []
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rows.append(json.loads(stripped))
                except json.JSONDecodeError:
                    continue
        return [_json_ready(row, secrets) for row in rows]

    def _raw(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool,
        idempotent: bool,
        timeout: float,
        base: str | None = None,
        bearer: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        root = (base or self._origin).rstrip("/")
        url = root + path
        service = "Venice" if "venice.ai" in root else "Surplus"
        if query:
            url = url + "?" + urllib.parse.urlencode(query)
        headers = {
            "Accept": "application/json, application/x-ndjson",
            "User-Agent": USER_AGENT,
        }
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        elif auth:
            headers["Authorization"] = f"Bearer {self.require_seller_key()}"
        if idempotent and method.upper() == "POST":
            headers["Idempotency-Key"] = str(uuid.uuid4())
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            status, response_headers, body = self._transport(request, timeout)
        except SurplusError:
            raise
        except Exception as exc:
            raise SurplusError(f"Network error calling {service}: {type(exc).__name__}") from None
        if len(body) > 2_000_000:
            raise SurplusError("Surplus response exceeded 2MB and was discarded.", status=status)
        if status >= 400:
            raise self._http_error(status, body, json_body, service=service)
        return status, response_headers, body

    def _body_secrets(self, json_body: dict[str, Any] | None) -> list[str]:
        secrets = self.known_secrets()
        if json_body:
            for key in ("api_key", "signature"):
                value = json_body.get(key)
                if isinstance(value, str):
                    secrets.append(value)
        return secrets

    def _http_error(
        self,
        status: int,
        body: bytes,
        json_body: dict[str, Any] | None,
        *,
        service: str = "Surplus",
    ) -> SurplusError:
        secrets = self._body_secrets(json_body)
        text = body.decode("utf-8", errors="replace")
        code = None
        detail = None
        message = f"{service} request failed (HTTP {status})."
        try:
            decoded = json.loads(text) if text else None
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            error = decoded.get("error", decoded)
            if isinstance(error, str):
                message = error
            elif isinstance(error, dict):
                message = str(error.get("message") or error.get("detail") or message)
                code = error.get("code") or error.get("type")
                detail = error.get("detail")
                if isinstance(detail, dict):
                    existing = detail.get("existing_offer_id") or detail.get("offer_id")
                    if existing:
                        message = f"{message} Existing offer: {existing}."
            elif isinstance(decoded.get("message"), str):
                message = decoded["message"]
        elif text.strip():
            message = text.strip()[:500]
        safe_detail = _strip_sensitive(_json_ready(detail, secrets)) if detail is not None else None
        return SurplusError(
            _redact(message, secrets),
            status=status,
            code=str(code) if code else None,
            detail=safe_detail,
        )


def urllib_transport(request: urllib.request.Request, timeout: float) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(2_000_001)
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, headers, body
    except urllib.error.HTTPError as exc:
        body = exc.read(2_000_001)
        headers = {key.lower(): value for key, value in exc.headers.items()} if exc.headers else {}
        return exc.code, headers, body


def _label(label: str | None) -> str:
    text = (label or PLUGIN_LABEL).strip()
    if not text or len(text) > 64:
        raise SurplusError("label must be 1–64 characters.")
    if any(ord(ch) < 32 for ch in text):
        raise SurplusError("label contains unsupported characters.")
    return text


def _prefix(key: str) -> str:
    if len(key) <= 20:
        return key[:8] + "…"
    return key[:20]


def _issued_key_payload(
    issued: Any,
    *,
    source: str,
    wallet: str | None,
    key: str | None = None,
) -> dict[str, Any]:
    data = issued if isinstance(issued, dict) else {}
    raw_key = key or str(data.get("key") or data.get("api_key") or "")
    if not raw_key.startswith("si_seller_"):
        raise SurplusError("Seller key issuance returned no si_seller_ key.")
    return {
        "ok": True,
        "source": source,
        "key": raw_key,
        "key_prefix": _prefix(raw_key),
        "id": data.get("id"),
        "wallet": wallet or data.get("wallet"),
        "label": data.get("label"),
        "private_key_stored": False,
        "persisted": False,
        "warning": (
            "Save this si_seller_ key as SURPLUS_SELLER_API_KEY in Hermes secrets now. "
            "It is shown once, is now in the conversation, and a restart drops the in-memory copy. "
            "The throwaway SIWE private key was discarded and holds no funds. "
            "USDC earnings settle only to SURPLUS_SELLER_PAYOUT_ADDRESS. "
            "Treat the seller key like a treasury credential: the Surplus API lets it redirect payouts. "
            "This plugin never calls the withdraw endpoint."
        ),
    }


def _public_offer(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return _strip_sensitive(data)


def _public_model(row: dict[str, Any]) -> dict[str, Any]:
    pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else None
    return {
        "model": row.get("model") or row.get("id"),
        "supported": row.get("supported", True),
        "pricing": pricing,
    }


def _money_fields(data: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    raw = {name: data.get(name) for name in names if name in data}
    usd = {name.replace("_usdc", "_usd"): micro_usdc_to_usd(data.get(name)) for name in names if name in data}
    return {"micro_usdc": raw, "usd": usd}


def _summarize_earnings(data: dict[str, Any]) -> dict[str, Any]:
    summary = _money_fields(
        data,
        ("total_earned_usdc", "pending_usdc", "in_flight_usdc", "paid_usdc", "cache_savings_usdc"),
    )
    summary["range"] = data.get("range")
    summary["computed_at"] = data.get("computed_at")
    share = data.get("share")
    summary["share"] = _strip_sensitive(share) if isinstance(share, dict) else None
    by_model = data.get("by_model") if isinstance(data.get("by_model"), list) else []
    summary["by_model"] = _strip_sensitive(by_model[:15])
    sales = data.get("recent_sales") if isinstance(data.get("recent_sales"), list) else []
    summary["recent_sales"] = _strip_sensitive(sales[:10])
    summary["payout_hold"] = data.get("payout_hold")
    return summary


def _summarize_payouts(data: dict[str, Any]) -> dict[str, Any]:
    summary = _money_fields(data, ("available_usdc", "in_flight_usdc", "paid_usdc", "threshold_usdc"))
    summary["mode"] = data.get("mode")
    summary["batched_enabled"] = data.get("batched_enabled")
    groups = data.get("groups") if isinstance(data.get("groups"), list) else []
    summary["groups"] = [
        {
            "recipient": group.get("recipient"),
            "recipient_source": group.get("recipient_source"),
            "available_usd": micro_usdc_to_usd(group.get("available_usdc")),
            "next_trigger": group.get("next_trigger"),
            "manually_claimable": group.get("manually_claimable"),
        }
        for group in groups[:10]
        if isinstance(group, dict)
    ]
    history = data.get("history") if isinstance(data.get("history"), list) else []
    summary["history"] = [
        {
            "status": entry.get("status"),
            "amount_usd": micro_usdc_to_usd(entry.get("amount_usdc")),
            "type": entry.get("type"),
            "tx_hash": entry.get("tx_hash"),
            "paid_at": entry.get("paid_at"),
        }
        for entry in history[:10]
        if isinstance(entry, dict)
    ]
    return summary


def _probe_payload(models: list[str], data: dict[str, Any]) -> dict[str, Any]:
    reachable = bool(data.get("ok", data.get("success")))
    return {
        "ok": reachable,
        "provider": "venice",
        "base_url": VENICE_BASE_URL,
        "model": models[0],
        "latency_ms": data.get("latency_ms"),
        "error": data.get("error"),
        "result": _strip_sensitive(data),
    }


_default_client: SurplusClient | None = None
_default_lock = threading.Lock()


def get_client() -> SurplusClient:
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = SurplusClient()
        return _default_client


def set_client(client: SurplusClient | None) -> None:
    global _default_client
    with _default_lock:
        _default_client = client
