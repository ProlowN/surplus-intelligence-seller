"""Seller tool handlers. Each returns a JSON string and accepts **kwargs."""

from __future__ import annotations

import json
from typing import Any

from . import fill
from .client import (
    VENICE_BASE_URL,
    SurplusError,
    _redact,
    get_client,
)

_PER_TOKEN_FIELDS = (
    "price_input_per_1m",
    "price_output_per_1m",
    "input_price",
    "output_price",
    "pricing_mode",
)


def _coerce_args(args: Any) -> dict[str, Any]:
    if args is None:
        return {}
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(args, dict):
        return args
    return {}


def _run(handler, args: Any, **kwargs: Any) -> str:
    del kwargs  # Hermes injects session context; handlers do not need it.
    try:
        payload = handler(_coerce_args(args))
        return json.dumps(payload, default=str)
    except SurplusError as exc:
        return json.dumps(exc.to_payload(), default=str)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        try:
            message = _redact(message, get_client().known_secrets())
        except Exception:
            message = type(exc).__name__
        return json.dumps({"ok": False, "error": message})


def _guard_venice_only(args: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if any(name in args for name in ("api_key", "venice_api_key", "provider_api_key")):
        notes.append("API key arguments are ignored. VENICE_API_KEY is read from Hermes secrets.")
    if any(name in args for name in _PER_TOKEN_FIELDS):
        if args.get("pricing_mode") not in (None, "cost_multiplier"):
            raise SurplusError(
                "Per-token pricing is retired. Pass cost_multiplier or discount_pct."
            )
        if any(name in args for name in _PER_TOKEN_FIELDS if name != "pricing_mode"):
            raise SurplusError(
                "Per-token pricing is retired. Pass cost_multiplier or discount_pct."
            )
    base = args.get("seller_base_url") or args.get("base_url") or args.get("provider")
    if base is not None:
        normalized = str(base).strip().rstrip("/")
        if normalized not in {VENICE_BASE_URL, "venice"}:
            raise SurplusError(
                "This plugin only sells Venice capacity. "
                f"The base URL is fixed to {VENICE_BASE_URL}."
            )
        notes.append(f"Base URL is fixed to {VENICE_BASE_URL}.")
    return notes


def _with_notes(payload: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    if not notes:
        return payload
    payload = dict(payload)
    payload["notes"] = list(payload.get("notes") or []) + notes
    return payload


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _max_models(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SurplusError("max_models must be an integer from 1 to 25.") from exc
    if not 1 <= number <= 25:
        raise SurplusError("max_models must be an integer from 1 to 25.")
    return number


def _model_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise SurplusError("models must be a list of Venice model ids.")


def surplus_seller_auth(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        _guard_venice_only(parsed)
        return get_client().mint_seller_key(
            label=parsed.get("label"),
            issue_additional=bool(parsed.get("issue_additional")),
        )

    return _run(handle, args, **kwargs)


def surplus_list_offers(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        notes = _guard_venice_only(parsed)
        status = parsed.get("status")
        model = parsed.get("model")
        return _with_notes(
            get_client().list_offers(
                status=str(status) if status else None,
                model=str(model) if model else None,
            ),
            notes,
        )

    return _run(handle, args, **kwargs)


def surplus_create_offer(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        notes = _guard_venice_only(parsed)
        return _with_notes(
            get_client().create_offer(
                model=parsed.get("model"),
                cost_multiplier=parsed.get("cost_multiplier"),
                discount_pct=parsed.get("discount_pct", parsed.get("discount")),
                cap_daily_usd=parsed.get("cap_daily_usd"),
            ),
            notes,
        )

    return _run(handle, args, **kwargs)


def surplus_update_offer(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        notes = _guard_venice_only(parsed)
        return _with_notes(
            get_client().update_offer(
                offer_id=parsed.get("offer_id"),
                cost_multiplier=parsed.get("cost_multiplier"),
                discount_pct=parsed.get("discount_pct", parsed.get("discount")),
                cap_daily_usd=parsed.get("cap_daily_usd"),
            ),
            notes,
        )

    return _run(handle, args, **kwargs)


def surplus_pause_or_cancel_offer(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        _guard_venice_only(parsed)
        return get_client().pause_or_cancel(
            offer_id=parsed.get("offer_id"),
            action=str(parsed.get("action") or ""),
        )

    return _run(handle, args, **kwargs)


def surplus_resume_offer(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        _guard_venice_only(parsed)
        return get_client().resume_offer(offer_id=parsed.get("offer_id"))

    return _run(handle, args, **kwargs)


def surplus_seller_earnings(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        _guard_venice_only(parsed)
        range_name = parsed.get("range") or "7d"
        return get_client().earnings(range_name=str(range_name))

    return _run(handle, args, **kwargs)


def surplus_discover_venice_models(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        notes = _guard_venice_only(parsed)
        limit = parsed.get("limit", 80)
        if limit is None:
            limit = 80
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise SurplusError("limit must be an integer from 1 to 200.") from exc
        return _with_notes(get_client().discover_models(limit=limit), notes)

    return _run(handle, args, **kwargs)


def surplus_test_venice_connection(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        notes = _guard_venice_only(parsed)
        models: list[str] = []
        single = parsed.get("model")
        if isinstance(single, str) and single.strip():
            models.append(single)
        many = parsed.get("models")
        if isinstance(many, str) and many.strip():
            models.append(many)
        elif isinstance(many, list):
            models.extend(str(item) for item in many)
        return _with_notes(get_client().test_connection(models=models), notes)

    return _run(handle, args, **kwargs)


def surplus_diem_capacity(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        _guard_venice_only(parsed)
        capacity = get_client().read_venice_diem()
        listed = get_client().list_offers()
        offers = listed.get("offers") or []
        suggested = capacity.get("suggested_cap_daily_usd")
        caps = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            caps.append(
                {
                    "offer_id": offer.get("id") or offer.get("offer_id"),
                    "model": offer.get("model"),
                    "status": offer.get("status"),
                    "healthy": offer.get("healthy"),
                    "cost_multiplier": offer.get("cost_multiplier"),
                    "cap_daily_usd": offer.get("cap_daily_usd"),
                    "cap_below_diem_budget": (
                        suggested is not None
                        and isinstance(offer.get("cap_daily_usd"), (int, float))
                        and float(offer["cap_daily_usd"]) + 0.009 < float(suggested)
                    ),
                }
            )
        capacity = dict(capacity)
        capacity["ok"] = True
        capacity["offers"] = caps
        return capacity

    return _run(handle, args, **kwargs)


def surplus_diem_utilization(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        _guard_venice_only(parsed)
        return fill.utilization(get_client())

    return _run(handle, args, **kwargs)


def surplus_cover_venice_models(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        _guard_venice_only(parsed)
        return fill.run_adjustment(
            get_client(),
            apply=_as_bool(parsed.get("apply"), True),
            max_new=_max_models(parsed.get("max_models"), 8),
            explicit_models=_model_list(parsed.get("models")),
            pause_unhealthy=False,
            pause_cold=False,
            discover_limit=200,
        )

    return _run(handle, args, **kwargs)


def surplus_rebalance_diem(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        _guard_venice_only(parsed)
        return fill.run_adjustment(
            get_client(),
            apply=_as_bool(parsed.get("apply"), False),
            max_new=_max_models(parsed.get("max_models"), 4),
            explicit_models=_model_list(parsed.get("models")),
            pause_unhealthy=True,
            pause_cold=_as_bool(parsed.get("pause_cold"), False),
            discover_limit=80,
        )

    return _run(handle, args, **kwargs)


def surplus_fill_remaining_diem(args: dict, **kwargs) -> str:
    def handle(parsed: dict[str, Any]) -> dict[str, Any]:
        _guard_venice_only(parsed)
        client = get_client()
        preview = client.read_venice_diem()
        risk = preview.get("leftover_diem_risk")
        default_max = 15 if risk == "critical" else 8
        pause_cold = risk in {"high", "critical"}
        return fill.run_adjustment(
            client,
            apply=_as_bool(parsed.get("apply"), True),
            max_new=_max_models(parsed.get("max_models"), default_max),
            explicit_models=_model_list(parsed.get("models")),
            pause_unhealthy=True,
            pause_cold=pause_cold,
            discover_limit=200,
        )

    return _run(handle, args, **kwargs)


def surplus_seller_command(raw_args: str = "", **kwargs) -> str:
    """Slash command `/surplus-seller`. Returns text for the chat, not JSON."""
    del kwargs
    arg = (raw_args or "").strip().lower()
    if arg in {"help", "-h", "--help"}:
        return (
            "Goal: sell all Venice DIEM capacity before 00:00 UTC. Leftover DIEM expires.\n"
            "Secrets: VENICE_API_KEY, SURPLUS_SELLER_PAYOUT_ADDRESS, "
            "optional SURPLUS_SELLER_API_KEY and VENICE_ADMIN_API_KEY.\n"
            "Daily loop: surplus_diem_capacity, surplus_rebalance_diem apply=true, "
            "and surplus_fill_remaining_diem late in the epoch.\n"
            "Usage: /surplus-seller [status|help]"
        )
    if arg not in {"", "status"}:
        return "Usage: /surplus-seller [status|help]"
    try:
        report = get_client().status_report()
    except SurplusError as exc:
        return f"Surplus seller status failed: {exc.message}"
    except Exception as exc:
        return f"Surplus seller status failed: {type(exc).__name__}"
    lines = [
        "Surplus seller status — Venice only. Sell today's Venice DIEM before 00:00 UTC",
        f"Venice base URL: {report.get('venice_base_url')}",
        (
            f"DIEM remaining ${report.get('diem_remaining_usd')} of "
            f"${report.get('diem_epoch_allocation_usd')} "
            f"({report.get('hours_left')}h left, risk {report.get('leftover_diem_risk')})"
            if report.get("leftover_diem_risk")
            else f"DIEM capacity: {report.get('diem_error') or 'unread'}"
        ),
        f"Seller key: {report.get('seller_key')}"
        + (f" ({report.get('seller_key_prefix')})" if report.get("seller_key_prefix") else ""),
        f"Venice key: {report.get('venice_key')}",
        f"Payout address: {report.get('payout_address')}",
        "Throwaway SIWE private key stored: no",
    ]
    if "offer_count" in report:
        lines.append(f"Offers: {report['offer_count']}")
        for offer in report.get("offers") or []:
            lines.append(
                f"- {offer.get('id') or offer.get('offer_id') or '(no id)'} "
                f"{offer.get('model')} status={offer.get('status')} "
                f"healthy={offer.get('healthy')} multiplier={offer.get('cost_multiplier')}"
            )
    if report.get("offers_error"):
        lines.append(f"Offers: {report['offers_error']}")
    if report.get("seller_key") == "missing":
        lines.append("Mint a seller key with surplus_seller_auth, then save it as SURPLUS_SELLER_API_KEY.")
    return "\n".join(lines)
