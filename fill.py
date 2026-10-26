"""Plan and apply daily DIEM sell-through. Venice offers only."""

from __future__ import annotations

from typing import Any

from . import strategy
from .client import SurplusError


def _hot_models(earnings: dict[str, Any] | None) -> list[str]:
    if not isinstance(earnings, dict):
        return []
    rows = earnings.get("by_model") if isinstance(earnings.get("by_model"), list) else []
    ranked: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("model"):
            continue
        amount = row.get("earned_usd")
        if amount is None:
            amount = row.get("earned_usdc")
        try:
            value = float(amount or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            ranked.append(str(row["model"]))
    return ranked


def _candidate_models(discovered: dict[str, Any] | None, explicit: list[str] | None) -> list[str]:
    if explicit:
        return [model.strip() for model in explicit if isinstance(model, str) and model.strip()]
    if not isinstance(discovered, dict):
        return []
    names: list[str] = []
    for row in discovered.get("models") or []:
        if isinstance(row, dict) and row.get("model"):
            names.append(str(row["model"]))
        elif isinstance(row, str) and row.strip():
            names.append(row.strip())
    return names


def load_context(client, *, explicit_models: list[str] | None, discover_limit: int) -> dict[str, Any]:
    capacity = client.read_venice_diem()
    offers_payload = client.list_offers()
    offers = offers_payload.get("offers") if isinstance(offers_payload, dict) else []
    if not isinstance(offers, list):
        offers = []
    earnings = None
    earnings_error = None
    try:
        earnings = client.request("GET", "/v1/seller/earnings", query={"range": "today"})
        if not _hot_models(earnings if isinstance(earnings, dict) else None):
            earnings = client.request("GET", "/v1/seller/earnings", query={"range": "7d"})
    except SurplusError as exc:
        earnings_error = exc.message
        earnings = None
    discovered = None
    discover_error = None
    if explicit_models is None:
        try:
            discovered = client.discover_models(limit=discover_limit)
        except SurplusError as exc:
            discover_error = exc.message
    return {
        "capacity": capacity,
        "offers": [offer for offer in offers if isinstance(offer, dict)],
        "hot_models": _hot_models(earnings if isinstance(earnings, dict) else None),
        "candidate_models": _candidate_models(discovered, explicit_models),
        "earnings_today_or_7d": earnings if isinstance(earnings, dict) else None,
        "earnings_error": earnings_error,
        "discover_error": discover_error,
    }


def build_plan(
    context: dict[str, Any],
    *,
    max_new: int,
    pause_unhealthy: bool,
    pause_cold: bool,
) -> dict[str, Any]:
    plan = strategy.plan_sellthrough(
        capacity=context["capacity"],
        offers=context["offers"],
        candidate_models=context["candidate_models"],
        hot_models=context["hot_models"],
        max_new=max_new,
        pause_unhealthy=pause_unhealthy,
        pause_cold=pause_cold,
    )
    plan["capacity"] = {
        key: context["capacity"].get(key)
        for key in (
            "diem_remaining_usd",
            "diem_epoch_allocation_usd",
            "diem_consumed_usd",
            "sell_through",
            "hours_left",
            "epoch_resets_at",
            "leftover_diem_risk",
            "suggested_cap_daily_usd",
            "sources",
        )
    }
    plan["hot_models"] = context["hot_models"]
    if context.get("earnings_error"):
        plan.setdefault("notes", []).append(context["earnings_error"])
    if context.get("discover_error"):
        plan.setdefault("notes", []).append(context["discover_error"])
    return plan


def apply_plan(client, plan: dict[str, Any]) -> list[dict[str, Any]]:
    actions = plan.get("actions") or []
    results: list[dict[str, Any]] = []
    creates = []
    for action in actions:
        kind = action.get("action")
        if kind == "update":
            try:
                updated = client.update_offer(
                    offer_id=action["offer_id"],
                    cost_multiplier=action.get("cost_multiplier"),
                    discount_pct=action.get("discount_pct"),
                    cap_daily_usd=action.get("cap_daily_usd"),
                    status="active" if action.get("resume") else None,
                )
                results.append({"ok": True, "action": "update", "model": action.get("model"), "result": updated})
            except SurplusError as exc:
                results.append(
                    {"ok": False, "action": "update", "model": action.get("model"), "error": exc.message}
                )
        elif kind == "create":
            creates.append(action)
        elif kind == "pause":
            try:
                paused = client.pause_or_cancel(offer_id=action["offer_id"], action="pause")
                results.append({"ok": True, "action": "pause", "model": action.get("model"), "result": paused})
            except SurplusError as exc:
                results.append(
                    {"ok": False, "action": "pause", "model": action.get("model"), "error": exc.message}
                )
    if creates:
        specs = []
        for action in creates:
            spec: dict[str, Any] = {
                "model": action["model"],
                "cost_multiplier": action.get("cost_multiplier"),
                "discount": action.get("discount_pct"),
            }
            if action.get("cap_daily_usd") is not None:
                spec["cap_daily_usd"] = action["cap_daily_usd"]
            specs.append(spec)
        for row in client.bulk_create_offers(specs):
            row = dict(row)
            row["action"] = "create"
            results.append(row)
    return results


def utilization(client) -> dict[str, Any]:
    """Sold DIEM versus the epoch allocation, plus today's Surplus earnings."""
    capacity = client.read_venice_diem()
    earnings_error = None
    earnings = None
    try:
        earnings = client.earnings(range_name="today")
    except SurplusError as exc:
        earnings_error = exc.message
    offers = []
    try:
        listed = client.list_offers()
        offers = listed.get("offers") or []
    except SurplusError as exc:
        capacity.setdefault("notes", []).append(exc.message)
    low_caps = []
    suggested = capacity.get("suggested_cap_daily_usd")
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        if str(offer.get("status") or "active").lower() in {"inactive", "paused"}:
            continue
        cap = offer.get("cap_daily_usd")
        try:
            cap_value = float(cap) if cap is not None else None
        except (TypeError, ValueError):
            cap_value = None
        if suggested is not None and cap_value is not None and cap_value + 0.009 < float(suggested):
            low_caps.append(
                {
                    "offer_id": offer.get("id") or offer.get("offer_id"),
                    "model": offer.get("model"),
                    "cap_daily_usd": cap_value,
                    "suggested_cap_daily_usd": suggested,
                }
            )
    surplus_today = None
    if isinstance(earnings, dict):
        usd = (earnings.get("earnings") or {}).get("usd") or {}
        surplus_today = usd.get("total_earned_usd")
    return {
        "ok": True,
        "goal": strategy.GOAL,
        "routine": strategy.ROUTINE,
        "diem_remaining_usd": capacity.get("diem_remaining_usd"),
        "diem_epoch_allocation_usd": capacity.get("diem_epoch_allocation_usd"),
        "diem_consumed_usd": capacity.get("diem_consumed_usd"),
        "sell_through": capacity.get("sell_through"),
        "hours_left": capacity.get("hours_left"),
        "epoch_resets_at": capacity.get("epoch_resets_at"),
        "leftover_diem_risk": capacity.get("leftover_diem_risk"),
        "leftover_diem_risk_meaning": capacity.get("leftover_diem_risk_meaning"),
        "suggested_cap_daily_usd": suggested,
        "low_caps": low_caps,
        "surplus_earned_today_usd": surplus_today,
        "recommendation": capacity.get("recommendation"),
        "sources": capacity.get("sources"),
        "notes": list(capacity.get("notes") or [])
        + (
            [
                "surplus_earned_today_usd is what Surplus accrued, in USD converted from micro-USD. "
                "It is not DIEM consumed. Discounted sales earn less than the DIEM they burn."
            ]
        ),
        "earnings_error": earnings_error,
        "active_offer_count": sum(
            1
            for offer in offers
            if isinstance(offer, dict) and str(offer.get("status") or "active").lower() == "active"
        ),
    }


def run_adjustment(
    client,
    *,
    apply: bool,
    max_new: int,
    explicit_models: list[str] | None,
    pause_unhealthy: bool,
    pause_cold: bool,
    discover_limit: int,
) -> dict[str, Any]:
    context = load_context(client, explicit_models=explicit_models, discover_limit=discover_limit)
    plan = build_plan(
        context,
        max_new=max_new,
        pause_unhealthy=pause_unhealthy,
        pause_cold=pause_cold,
    )
    payload: dict[str, Any] = {
        "ok": True,
        "applied": False,
        "goal": strategy.GOAL,
        "routine": strategy.ROUTINE,
        **plan,
    }
    if apply:
        payload["applied"] = True
        payload["results"] = apply_plan(client, plan)
    else:
        payload["note"] = "Dry run. Pass apply=true to reprice, raise caps, create coverage, and pause dead offers."
    return payload
