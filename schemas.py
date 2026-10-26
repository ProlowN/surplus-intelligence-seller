"""Tool schemas for the Venice-only Surplus Intelligence seller plugin."""

SURPLUS_SELLER_AUTH = {
    "name": "surplus_seller_auth",
    "description": (
        "Mint a Surplus Intelligence seller API key (si_seller_…) by signing a SIWE "
        "challenge with a throwaway wallet. The private key is discarded immediately "
        "and is never stored; USDC earnings settle to SURPLUS_SELLER_PAYOUT_ADDRESS, "
        "not to that wallet. If a seller key is already configured, this does not mint "
        "a new seller identity. Pass issue_additional=true to create another key on "
        "the same account. The new key is returned once — save it as "
        "SURPLUS_SELLER_API_KEY. Does not buy inference or touch buyer balance."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "description": "Optional label stored on the seller key (max 64 characters).",
            },
            "issue_additional": {
                "type": "boolean",
                "description": (
                    "When a seller key already exists, mint another si_seller_ key "
                    "on that same seller account instead of refusing."
                ),
            },
        },
        "required": [],
    },
}

SURPLUS_LIST_OFFERS = {
    "name": "surplus_list_offers",
    "description": (
        "List this seller's Surplus Intelligence offers for the Venice key. "
        "One active offer is allowed per (Venice API key, https://api.venice.ai/api/v1, model). "
        "Paused offers are stored as inactive. Does not list buyer products or other providers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "paused", "inactive"],
                "description": "Optional filter. paused is queried as inactive because that is how Surplus stores a pause.",
            },
            "model": {
                "type": "string",
                "description": "Optional Venice model id filter.",
            },
        },
        "required": [],
    },
}

SURPLUS_CREATE_OFFER = {
    "name": "surplus_create_offer",
    "description": (
        "Create one Surplus seller offer that resells Venice DIEM-funded capacity. "
        "Goal: sell the whole epoch allowance before 00:00 UTC; leftover DIEM expires. "
        "Always uses seller_base_url https://api.venice.ai/api/v1, the VENICE_API_KEY secret, "
        "and SURPLUS_SELLER_PAYOUT_ADDRESS. If cost_multiplier and discount_pct are omitted, "
        "the price is the utilization-first schedule (opening 20% off, deeper when sell-through "
        "is behind the clock). If cap_daily_usd is omitted, it is set to the DIEM epoch "
        "allocation when Venice reports one, so a low cap cannot strand the day. Per-token "
        "prices are retired. One active offer per (Venice key, Venice base URL, model)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "description": "Venice model id to list, for example llama-3.3-70b.",
            },
            "cost_multiplier": {
                "type": "number",
                "description": "Fraction of the Surplus reference price to charge, from 0 to 2. Use this or discount_pct.",
            },
            "discount_pct": {
                "type": "number",
                "description": "Percent off the reference price, from 0 to 100. 15 means cost_multiplier 0.85. A trailing % is accepted.",
            },
            "cap_daily_usd": {
                "type": "number",
                "description": "Optional daily USD cap for this offer.",
            },
        },
        "required": ["model"],
    },
}

SURPLUS_UPDATE_OFFER = {
    "name": "surplus_update_offer",
    "description": (
        "Reprice a Venice offer or change its daily USD cap via PATCH /v1/seller/offers/{id}. "
        "Pricing is cost_multiplier or discount_pct only. Does not change the provider, "
        "base URL, Venice key, or payout wallet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "offer_id": {
                "type": "string",
                "description": "Surplus offer id to update.",
            },
            "cost_multiplier": {
                "type": "number",
                "description": "New cost multiplier from 0 to 2.",
            },
            "discount_pct": {
                "type": "number",
                "description": "New percent off the reference price, from 0 to 100.",
            },
            "cap_daily_usd": {
                "type": "number",
                "description": "New daily USD cap. Must be greater than 0.",
            },
        },
        "required": ["offer_id"],
    },
}

SURPLUS_PAUSE_OR_CANCEL_OFFER = {
    "name": "surplus_pause_or_cancel_offer",
    "description": (
        "Pause or cancel one Venice seller offer. pause PATCHes status to paused "
        "(Surplus stores that as inactive and stops routing; price and config are kept). "
        "cancel sends DELETE, a soft delete. Revoke the Venice key at Venice for immediate "
        "invalidation. Use surplus_resume_offer to set status back to active."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "offer_id": {
                "type": "string",
                "description": "Surplus offer id.",
            },
            "action": {
                "type": "string",
                "enum": ["pause", "cancel"],
                "description": "pause keeps the offer for later resume; cancel soft-deletes it.",
            },
        },
        "required": ["offer_id", "action"],
    },
}

SURPLUS_RESUME_OFFER = {
    "name": "surplus_resume_offer",
    "description": (
        "Relist a paused or cancelled Venice offer by PATCHing status to active. "
        "Fails with 409 if another active offer already uses this Venice key, "
        "https://api.venice.ai/api/v1, and model. Fails with 400 offer_delisted_by_operator "
        "when Surplus has permanently delisted the offer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "offer_id": {
                "type": "string",
                "description": "Surplus offer id to relist.",
            },
        },
        "required": ["offer_id"],
    },
}

SURPLUS_SELLER_EARNINGS = {
    "name": "surplus_seller_earnings",
    "description": (
        "Summarize Surplus seller earnings and the USDC-on-Base payout position for the "
        "Venice capacity you listed. Amounts on the wire are integer micro-USD strings; "
        "the result also includes decimal USD. Earnings accrue per request; payouts batch. "
        "Confirm payment from payout history tx hashes, not from per-request status accrued. "
        "Does not withdraw, redirect payouts, or show buyer balances."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "range": {
                "type": "string",
                "enum": ["today", "7d", "30d", "90d", "mtd", "lifetime"],
                "description": "Earnings window. Defaults to 7d. Payout totals are account-level either way.",
            },
        },
        "required": [],
    },
}

SURPLUS_DISCOVER_VENICE_MODELS = {
    "name": "surplus_discover_venice_models",
    "description": (
        "Discover which models the configured Venice key can sell on Surplus Intelligence. "
        "Calls POST /v1/seller/discover with https://api.venice.ai/api/v1 only. "
        "Use the returned model ids with surplus_create_offer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum supported models to return (1–200, default 80).",
            },
        },
        "required": [],
    },
}

SURPLUS_TEST_VENICE_CONNECTION = {
    "name": "surplus_test_venice_connection",
    "description": (
        "Probe the Venice key through Surplus before listing it. One model uses "
        "POST /v1/seller/test-connection. Several models use POST /v1/seller/test-health. "
        "The base URL is always https://api.venice.ai/api/v1. This spends a tiny amount "
        "of Venice quota on the probe."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "description": "One Venice model id to probe.",
            },
            "models": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Several Venice model ids to probe together.",
            },
        },
        "required": [],
    },
}

SURPLUS_DIEM_CAPACITY = {
    "name": "surplus_diem_capacity",
    "description": (
        "Read how much Venice DIEM is left today and whether offer caps can actually sell it. "
        "Uses GET /api/v1/billing/balance (remaining DIEM plus diemEpochAllocation; admin key "
        "may be required) and GET /api/v1/api_keys/rate_limits on the inference key (remaining "
        "DIEM and nextEpochBegins). Also lists Surplus offers and flags caps below the epoch "
        "budget. Returns hours until the UTC reset, a utilization-first price, and "
        "leftover_diem_risk (none, watch, high, critical, unknown). Read-only. "
        "1 DIEM ≈ $1 this epoch; unused allowance does not roll over."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

SURPLUS_DIEM_UTILIZATION = {
    "name": "surplus_diem_utilization",
    "description": (
        "Telemetry for the current Venice DIEM epoch: allocation, remaining, consumed, "
        "sell-through, hours left, leftover DIEM risk, and today's Surplus earnings. "
        "Consumed DIEM is the Venice balance change. Surplus earnings are lower when "
        "offers are discounted. Call this through the day. Leftover DIEM at 00:00 UTC is lost income."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

SURPLUS_COVER_VENICE_MODELS = {
    "name": "surplus_cover_venice_models",
    "description": (
        "Discover Venice models and keep active Surplus offers on them so DIEM is not stuck "
        "on a dead model. Creates missing offers at the utilization-first price with "
        "cap_daily_usd equal to the DIEM epoch budget, resumes inactive offers for those "
        "models, and sets the payout wallet on every new offer. Pass models to limit the set. "
        "apply defaults to true. One active offer per (Venice key, Venice base URL, model)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "models": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Venice model ids to cover. Omit to use discovery, hot models first.",
            },
            "max_models": {
                "type": "integer",
                "description": "Maximum new offers to create (1–25, default 8).",
            },
            "apply": {
                "type": "boolean",
                "description": "Create and update offers. Default true. false returns the plan only.",
            },
        },
        "required": [],
    },
}

SURPLUS_REBALANCE_DIEM = {
    "name": "surplus_rebalance_diem",
    "description": (
        "Mid-day DIEM sell-through check. Compares DIEM left and hours to 00:00 UTC with "
        "offer price, health, and caps. The plan deepens discounts when sell-through is behind "
        "(it never raises price), raises low cap_daily_usd up to the epoch allocation, "
        "adds missing models, and pauses unhealthy offers. Default apply=false (dry run). "
        "A routine should call apply=true several times per UTC day, more often late in the epoch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "apply": {
                "type": "boolean",
                "description": "Execute the plan. Default false.",
            },
            "max_models": {
                "type": "integer",
                "description": "Maximum new model offers to add (1–25, default 4).",
            },
            "pause_cold": {
                "type": "boolean",
                "description": "Pause active models with no recent earnings when another model is selling. Default false.",
            },
            "models": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional Venice model ids to consider for new coverage.",
            },
        },
        "required": [],
    },
}

SURPLUS_FILL_REMAINING_DIEM = {
    "name": "surplus_fill_remaining_diem",
    "description": (
        "End-of-epoch workflow: sell down remaining Venice DIEM before 00:00 UTC. "
        "When leftover_diem_risk is high or critical, deepen the discount (up to 75% off "
        "in the last hour), widen the model set, raise caps to the epoch allocation, and "
        "pause unhealthy or cold offers. apply defaults to true. No-op when DIEM remaining "
        "is about $0. Does not buy inference and does not withdraw USDC."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "apply": {
                "type": "boolean",
                "description": "Execute the plan. Default true.",
            },
            "max_models": {
                "type": "integer",
                "description": "Maximum new offers when widening coverage (1–25). Default 8, or 15 when risk is critical if omitted.",
            },
            "models": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional Venice model ids to add if they are not already listed.",
            },
        },
        "required": [],
    },
}

SCHEMAS = {
    "surplus_seller_auth": SURPLUS_SELLER_AUTH,
    "surplus_list_offers": SURPLUS_LIST_OFFERS,
    "surplus_create_offer": SURPLUS_CREATE_OFFER,
    "surplus_update_offer": SURPLUS_UPDATE_OFFER,
    "surplus_pause_or_cancel_offer": SURPLUS_PAUSE_OR_CANCEL_OFFER,
    "surplus_resume_offer": SURPLUS_RESUME_OFFER,
    "surplus_seller_earnings": SURPLUS_SELLER_EARNINGS,
    "surplus_discover_venice_models": SURPLUS_DISCOVER_VENICE_MODELS,
    "surplus_test_venice_connection": SURPLUS_TEST_VENICE_CONNECTION,
    "surplus_diem_capacity": SURPLUS_DIEM_CAPACITY,
    "surplus_diem_utilization": SURPLUS_DIEM_UTILIZATION,
    "surplus_cover_venice_models": SURPLUS_COVER_VENICE_MODELS,
    "surplus_rebalance_diem": SURPLUS_REBALANCE_DIEM,
    "surplus_fill_remaining_diem": SURPLUS_FILL_REMAINING_DIEM,
}
