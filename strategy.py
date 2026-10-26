"""Utilization-first pricing for a Venice DIEM epoch.

1 staked DIEM is about $1 of Venice credit for the current epoch. Unused
allowance does not roll over: it resets at 00:00 UTC. Seller revenue is
maximized by selling the allowance down to ~0 before that reset, even when
that means a deeper discount than the opening price.

Automatic actions only move price in the cheaper direction. A caller can still
set an explicit multiplier.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

GOAL = (
    "Sell all Venice DIEM-funded capacity before the epoch resets at 00:00 UTC. "
    "Leftover DIEM does not roll over and is lost income. Prefer selling out "
    "over leaving margin on idle capacity."
)

# Opening price is already a discount: win flow at the start of the day.
# Later bands only get cheaper. 0.25 is the automatic floor (75% off); a
# free listing is not the default because a sale at 0.25 still earns and a
# 0.00 listing does not.
OPENING_DISCOUNT_PCT = 20.0
BEHIND_DISCOUNT_PCT = 30.0
LATE_DISCOUNT_PCT = 45.0
URGENT_DISCOUNT_PCT = 60.0
CLEARANCE_DISCOUNT_PCT = 75.0

ROUTINE = (
    "Run surplus_diem_capacity and surplus_rebalance_diem apply=true several "
    "times each UTC day. In the last 6 hours, and whenever leftover_diem_risk "
    "is watch, high, or critical, run surplus_fill_remaining_diem. After 00:00 UTC "
    "the unsold allowance is gone."
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _discount_result(discount: float, reason: str) -> dict[str, Any]:
    multiplier = round(1 - (discount / 100), 6)
    return {
        "discount_pct": discount,
        "cost_multiplier": multiplier,
        "reason": reason,
        "policy": "utilization_first",
        "tradeoff": (
            "A higher discount wins routing and burns DIEM that would otherwise "
            "expire. A lower discount keeps margin and risks leftover DIEM worth $0. "
            "This policy defaults to utilization."
        ),
    }


def recommend_discount(
    *,
    hours_left: float,
    sell_through: float | None,
    remaining_usd: float | None,
    allocation_usd: float | None,
) -> dict[str, Any]:
    """Pick a discount that gets more aggressive as the epoch runs out unsold."""
    remaining_fraction = None
    if allocation_usd and allocation_usd > 0 and remaining_usd is not None:
        remaining_fraction = max(0.0, min(1.0, remaining_usd / allocation_usd))
    elapsed = max(0.0, min(1.0, 1 - (hours_left / 24)))
    behind = None
    if sell_through is not None:
        behind = elapsed - max(0.0, min(1.0, sell_through))

    still_there = remaining_usd is None or remaining_usd > 0.5
    if hours_left <= 1 and still_there and (remaining_fraction is None or remaining_fraction > 0.05):
        return _discount_result(
            CLEARANCE_DISCOUNT_PCT,
            "Last hour of the UTC epoch and DIEM is still unsold. Clear it.",
        )
    if (
        hours_left <= 3
        and still_there
        and (remaining_usd or 0) > 1
        and (remaining_fraction is None or remaining_fraction > 0.15)
    ):
        return _discount_result(
            URGENT_DISCOUNT_PCT,
            "Under 3 hours to epoch reset and a material DIEM balance remains.",
        )
    if (behind is not None and behind >= 0.15) or (
        hours_left <= 6 and remaining_fraction is not None and remaining_fraction > 0.25
    ):
        return _discount_result(
            LATE_DISCOUNT_PCT,
            "Sell-through is behind the clock, or too much of the allowance is unsold late in the epoch.",
        )
    if (behind is not None and behind > 0.05) or (
        hours_left <= 12 and remaining_fraction is not None and remaining_fraction > 0.40
    ):
        return _discount_result(
            BEHIND_DISCOUNT_PCT,
            "Consumed DIEM is behind elapsed epoch time. Discount enough to catch up.",
        )
    return _discount_result(
        OPENING_DISCOUNT_PCT,
        "Opening utilization price, 20% under the Surplus reference, so the day starts with volume.",
    )


def leftover_risk(
    *,
    hours_left: float,
    remaining_usd: float | None,
    allocation_usd: float | None,
) -> str:
    """none, watch, high, critical, or unknown."""
    if remaining_usd is None:
        return "unknown"
    if remaining_usd <= 0.05:
        return "none"
    fraction = (remaining_usd / allocation_usd) if allocation_usd and allocation_usd > 0 else None
    if hours_left <= 3 and remaining_usd >= 1 and (fraction is None or fraction > 0.20):
        return "critical"
    if hours_left <= 8 and remaining_usd >= 1 and (fraction is None or fraction > 0.40):
        return "high"
    if fraction is not None:
        elapsed = max(0.0, min(1.0, 1 - (hours_left / 24)))
        sell_through = 1 - fraction
        if elapsed - sell_through > 0.10:
            return "watch"
    elif hours_left <= 12 and remaining_usd >= 5:
        return "watch"
    return "none"


def suggested_daily_cap(allocation_usd: float | None, remaining_usd: float | None) -> float | None:
    """Ceiling high enough for the whole day's DIEM, never a token stub amount.

    Prefer the epoch allocation. A cap equal to today's *remaining* balance can
    sit under spend Surplus has already counted and stop routing early. When
    the allocation is unknown, remaining DIEM is the only budget we can see.
    """
    if allocation_usd is not None and allocation_usd > 0:
        return round(float(allocation_usd), 2)
    if remaining_usd is not None and remaining_usd > 0:
        return round(float(remaining_usd), 2)
    return None


def next_epoch_end(now: datetime, next_epoch_iso: str | None = None) -> datetime:
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    if isinstance(next_epoch_iso, str) and next_epoch_iso.strip():
        text = next_epoch_iso.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    return (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def epoch_clock(now: datetime, next_epoch_iso: str | None = None) -> dict[str, Any]:
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    end = next_epoch_end(current, next_epoch_iso)
    hours_left = max(0.0, (end - current).total_seconds() / 3600)
    return {
        "now_utc": current.isoformat(),
        "epoch_resets_at": end.isoformat(),
        "hours_left": round(hours_left, 4),
        "epoch_reset": "00:00 UTC unless Venice nextEpochBegins says otherwise",
    }


def interpret_venice(
    billing: dict[str, Any] | None,
    rate_limits: dict[str, Any] | None,
    now: datetime,
    *,
    billing_error: str | None = None,
    rate_error: str | None = None,
    billing_source: str | None = None,
) -> dict[str, Any]:
    """Merge GET /billing/balance and GET /api_keys/rate_limits into one snapshot."""
    remaining = None
    allocation = None
    consumption_currency = None
    can_consume = None
    if isinstance(billing, dict):
        balances = billing.get("balances") if isinstance(billing.get("balances"), dict) else {}
        remaining = _number(balances.get("diem"))
        allocation = _number(billing.get("diemEpochAllocation"))
        consumption_currency = billing.get("consumptionCurrency")
        can_consume = billing.get("canConsume")
    next_epoch = None
    key_diem = None
    if isinstance(rate_limits, dict):
        data = rate_limits.get("data") if isinstance(rate_limits.get("data"), dict) else rate_limits
        if isinstance(data, dict):
            rate_balances = data.get("balances") if isinstance(data.get("balances"), dict) else {}
            key_diem = _number(rate_balances.get("DIEM"))
            if remaining is None:
                remaining = key_diem
            next_epoch = data.get("nextEpochBegins") if isinstance(data.get("nextEpochBegins"), str) else None
    clock = epoch_clock(now, next_epoch)
    consumed = None
    sell_through = None
    if allocation is not None and remaining is not None and allocation > 0:
        consumed = round(max(0.0, allocation - remaining), 6)
        sell_through = round(consumed / allocation, 6)
    recommendation = recommend_discount(
        hours_left=clock["hours_left"],
        sell_through=sell_through,
        remaining_usd=remaining,
        allocation_usd=allocation,
    )
    risk = leftover_risk(
        hours_left=clock["hours_left"],
        remaining_usd=remaining,
        allocation_usd=allocation,
    )
    sources = []
    if isinstance(billing, dict) and (remaining is not None or allocation is not None):
        sources.append(billing_source or "billing/balance")
    if key_diem is not None or next_epoch:
        sources.append("api_keys/rate_limits")
    notes = [
        "balances.diem and balances.DIEM are USD of DIEM credit still spendable this epoch.",
        "diemEpochAllocation is the full epoch budget from staked DIEM (about $1 per DIEM).",
        "GET /billing/balance returns both figures and may require a Venice admin key. "
        "An inference key often receives 401 there. GET /api_keys/rate_limits works with "
        "the inference key and returns remaining DIEM plus nextEpochBegins, not the allocation.",
        "DIEM consumed is the Venice balance change, not Surplus payout revenue. "
        "Revenue is lower than DIEM consumed when offers are discounted.",
    ]
    if allocation is None:
        notes.append(
            "Epoch allocation is unknown, so sell-through percent is unknown. "
            "Set optional VENICE_ADMIN_API_KEY (admin key on the same Venice account) "
            "to read diemEpochAllocation. Remaining DIEM still drives urgency."
        )
    if billing_error:
        notes.append(f"billing/balance: {billing_error}")
    if rate_error:
        notes.append(f"api_keys/rate_limits: {rate_error}")
    return {
        "goal": GOAL,
        "routine": ROUTINE,
        "provider": "venice",
        "sources": sources,
        "diem_remaining_usd": remaining,
        "diem_epoch_allocation_usd": allocation,
        "diem_consumed_usd": consumed,
        "sell_through": sell_through,
        "key_diem_balance_usd": key_diem,
        "consumption_currency": consumption_currency,
        "can_consume": can_consume,
        "suggested_cap_daily_usd": suggested_daily_cap(allocation, remaining),
        "recommendation": recommendation,
        "leftover_diem_risk": risk,
        "leftover_diem_risk_meaning": _risk_meaning(risk),
        **clock,
        "notes": notes,
    }


def _risk_meaning(risk: str) -> str:
    return {
        "none": "DIEM remaining is ~0, or sell-through is keeping up with the clock.",
        "watch": "Sell-through is behind elapsed time. Reprice cheaper and check caps before the evening.",
        "high": "A large share of today's DIEM is still unsold with 8 hours or less left.",
        "critical": "The epoch resets within 3 hours and DIEM remains. Deepen the discount and widen coverage now.",
        "unknown": "Venice did not return a remaining DIEM balance, so leftover risk cannot be scored.",
    }.get(risk, risk)


def _offer_model(offer: dict[str, Any]) -> str | None:
    model = offer.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _offer_identity(offer: dict[str, Any]) -> str | None:
    for key in ("offer_id", "id"):
        value = offer.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_active(offer: dict[str, Any]) -> bool:
    status = str(offer.get("status") or "").lower()
    if status in {"inactive", "paused", "deleted", "cancelled", "canceled"}:
        return False
    if offer.get("active") is False:
        return False
    return True


def _multiplier_of(offer: dict[str, Any]) -> float | None:
    return _number(offer.get("cost_multiplier"))


def _cap_of(offer: dict[str, Any]) -> float | None:
    return _number(offer.get("cap_daily_usd"))


def plan_sellthrough(
    *,
    capacity: dict[str, Any],
    offers: list[dict[str, Any]],
    candidate_models: list[str],
    hot_models: list[str],
    max_new: int,
    pause_unhealthy: bool,
    pause_cold: bool,
) -> dict[str, Any]:
    """Decide create / reprice / raise-cap / pause actions. Does not call the network.

    Repricing only deepens the discount. Caps move up to the DIEM budget, never down.
    """
    remaining = _number(capacity.get("diem_remaining_usd"))
    if remaining is not None and remaining <= 0.05:
        return {
            "goal": GOAL,
            "actions": [],
            "summary": "DIEM remaining is about $0. This epoch is sold through.",
            "target": capacity.get("recommendation"),
            "suggested_cap_daily_usd": capacity.get("suggested_cap_daily_usd"),
        }

    recommendation = capacity.get("recommendation") if isinstance(capacity.get("recommendation"), dict) else {}
    target_discount = _number(recommendation.get("discount_pct")) or OPENING_DISCOUNT_PCT
    target_multiplier = _number(recommendation.get("cost_multiplier"))
    if target_multiplier is None:
        target_multiplier = round(1 - (target_discount / 100), 6)
    suggested_cap = _number(capacity.get("suggested_cap_daily_usd"))
    hot = set(hot_models)

    by_model: dict[str, dict[str, Any]] = {}
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        model = _offer_model(offer)
        if not model:
            continue
        current = by_model.get(model)
        if current is None or (_is_active(offer) and not _is_active(current)):
            by_model[model] = offer

    actions: list[dict[str, Any]] = []
    active_models = [model for model, offer in by_model.items() if _is_active(offer)]
    for model, offer in by_model.items():
        offer_id = _offer_identity(offer)
        if not offer_id:
            continue
        active = _is_active(offer)
        unhealthy = offer.get("healthy") is False
        if not active and unhealthy:
            continue
        cold = (
            pause_cold
            and active
            and not unhealthy
            and model not in hot
            and len(active_models) > 1
            and len(hot) > 0
        )
        if active and unhealthy and pause_unhealthy:
            actions.append(
                {
                    "action": "pause",
                    "offer_id": offer_id,
                    "model": model,
                    "reason": "Offer is unhealthy and is not delivering the DIEM allowance. Pause it and cover a model that can.",
                }
            )
            continue
        if cold:
            actions.append(
                {
                    "action": "pause",
                    "offer_id": offer_id,
                    "model": model,
                    "reason": "No recent earnings on this model while other models are selling and the epoch is almost over.",
                }
            )
            continue
        current_multiplier = _multiplier_of(offer)
        deeper = current_multiplier is None or current_multiplier > target_multiplier + 1e-6
        current_cap = _cap_of(offer)
        raise_cap = False
        if suggested_cap is not None:
            if current_cap is None:
                raise_cap = True
            elif current_cap + 0.009 < suggested_cap:
                raise_cap = True
        if not active or deeper or raise_cap:
            action: dict[str, Any] = {
                "action": "update",
                "offer_id": offer_id,
                "model": model,
                "resume": not active,
                "reason": [],
            }
            if not active:
                action["reason"].append("Relist this model so it can take flow again.")
            if deeper or not active:
                action["cost_multiplier"] = target_multiplier
                action["discount_pct"] = target_discount
                action["reason"].append(
                    "Move to the utilization price. Automatic repricing never makes an offer more expensive."
                )
            if raise_cap or not active:
                if suggested_cap is not None:
                    action["cap_daily_usd"] = suggested_cap
                    action["reason"].append(
                        "Raise cap_daily_usd to the DIEM epoch budget so a low cap cannot strand allowance."
                    )
            action["reason"] = " ".join(action["reason"])
            actions.append(action)

    covered = set(by_model)
    ordered: list[str] = []
    for model in list(hot_models) + list(candidate_models):
        if model and model not in ordered:
            ordered.append(model)
    fresh = [model for model in ordered if model not in covered][: max(0, max_new)]
    for model in fresh:
        created: dict[str, Any] = {
            "action": "create",
            "model": model,
            "cost_multiplier": target_multiplier,
            "discount_pct": target_discount,
            "reason": "No offer for this Venice model. Coverage keeps DIEM from sitting on models buyers are not requesting.",
        }
        if suggested_cap is not None:
            created["cap_daily_usd"] = suggested_cap
        actions.append(created)

    if not actions:
        summary = "Offers already match the utilization price and the DIEM cap. Keep checking until the epoch resets."
    else:
        summary = (
            f"{sum(1 for item in actions if item['action'] == 'create')} to create, "
            f"{sum(1 for item in actions if item['action'] == 'update')} to reprice or raise cap, "
            f"{sum(1 for item in actions if item['action'] == 'pause')} to pause."
        )
    return {
        "goal": GOAL,
        "actions": actions,
        "summary": summary,
        "target": recommendation,
        "suggested_cap_daily_usd": suggested_cap,
        "leftover_diem_risk": capacity.get("leftover_diem_risk"),
    }
