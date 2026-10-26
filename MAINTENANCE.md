# Maintenance

Checklist for when the Surplus seller API or Venice DIEM rules change. The product goal is to sell the **entire daily DIEM allowance** on Venice before 00:00 UTC. This plugin only sells Venice capacity (`https://api.venice.ai/api/v1`). It does not shop for inference or top up a buyer balance.

## Sell-through policy

The bands live in `strategy.py` (`OPENING_DISCOUNT_PCT` through `CLEARANCE_DISCOUNT_PCT`). Automatic plans only move price down. `suggested_daily_cap` uses epoch allocation when Venice returns it.

- [ ] If Venice changes the epoch boundary, update `next_epoch_end` and the README. Prefer `nextEpochBegins` from `GET /api_keys/rate_limits` over a hard-coded midnight when the field is present.
- [ ] If `GET /billing/balance` starts working for inference keys, or stops returning `balances.diem` / `diemEpochAllocation`, update `read_venice_diem`. Today an inference key often gets 401; `VENICE_ADMIN_API_KEY` is optional for that route.
- [ ] If `GET /api_keys/rate_limits` renames `balances.DIEM` or `nextEpochBegins`, update `interpret_venice`.
- [ ] Re-check that a low `cap_daily_usd` still stops Surplus routing. The default cap must stay at the full epoch budget, not a token amount and not "remaining" when allocation is known.
- [ ] Bulk create (`POST /v1/seller/offers/bulk`) still has no payout field. Keep the payout PATCH, and keep cancelling the offer if that PATCH fails.

## Watch these sources

- https://www.surplusintelligence.ai/llms.txt — authoritative API. Re-read “Before you write anything” (Idempotency-Key routes and micro-USD units) and “Selling capacity”.
- https://www.surplusintelligence.ai/docs/api-reference/seller-endpoints — offer lifecycle, discover, test-connection, earnings, payouts.
- https://www.surplusintelligence.ai/docs/getting-started/seller-quickstart — seller flow and the one-offer-per-triple rule.
- https://docs.venice.ai/overview/vvv-diem — DIEM allowance, epoch reset, spend order, and whether unused allowance rolls over.
- https://docs.venice.ai/api-reference/endpoint/billing/balance — remaining DIEM and `diemEpochAllocation`.
- https://docs.venice.ai/api-reference/endpoint/api_keys/rate_limits — key DIEM balance and `nextEpochBegins` for inference keys.
- https://docs.venice.ai/llms.txt — if the Venice API host or model-list path changes.
- Reference seller HTTP shapes (do not copy the OpenClaw product): https://github.com/ProlowN/openclaw-plugin-surplus-intelligence (`seller-actions.js`, `auth.js`, `http.js`, Venice entry in `providers.js`).

## When Surplus changes

- [ ] Idempotency-Key required set. Creates must send it on `POST /v1/seller/offers` and `POST /v1/seller/keys`. `POST /v1/seller/auth/keys` currently accepts it and does not require it. `PATCH` and `DELETE /v1/seller/offers/{id}` currently ignore it.
- [ ] Pricing. Text offers must stay on `cost_multiplier` (0–2 on create) and/or `discount` (0–100). If per-token fields (`price_input_per_1m`, `price_output_per_1m`) come back, or the multiplier/discount split between text and media changes, update `parse_pricing` and the tool descriptions. `PATCH` has historically not enforced the create bounds.
- [ ] Uniqueness. One active offer per `(api_key, seller_base_url, model)`. Conflicts are `409 duplicate_offer_for_credential`, with `error.detail.existing_offer_id` when the offer is yours. Resume and credential rotation re-check the same triple.
- [ ] Pause / resume. Pause is `PATCH {"status":"paused"}` and is stored as `inactive`. Resume is `PATCH {"status":"active"}`. Cancel is `DELETE` (soft delete). Operator delist is `400 offer_delisted_by_operator` and is not reversible by resume.
- [ ] Discover and probes. `POST /v1/seller/discover` and `POST /v1/seller/test-health` may stay NDJSON. `POST /v1/seller/test-connection` is one JSON object. Keep the Venice base URL hard-coded; do not accept another provider.
- [ ] Money fields. Seller `*_usdc` values are integer micro-USD strings unless llms.txt says otherwise. Earnings accrue per request; payouts batch. Do not treat per-request `accrued` as unpaid. Payment is a payout history row with `status: paid` and a Base `tx_hash`. Read the live threshold from `threshold_usdc` instead of hard-coding 5 USDC.
- [ ] Auth. `GET /v1/seller/auth/challenge` then `POST /v1/seller/auth/keys`. A new SIWE wallet is a new seller identity. Additional keys on the same account are `POST /v1/seller/keys` (Idempotency-Key required).
- [ ] Payout redirect. `POST /v1/seller/payouts/withdraw` can override the recipient. This plugin must keep **not** calling withdraw unless a future change explicitly adds that tool and documents it as moving funds.
- [ ] Error envelope and rate-limit headers (`Retry-After`, `X-RateLimit-*`). Surface `code` and `existing_offer_id`. Do not log request bodies.

## When Venice DIEM changes

- [ ] Confirm 1 staked DIEM still equals $1/day of Venice credit and that unused epoch allowance still does not roll over. Update the README and `plugin.yaml` descriptions if the rule changes.
- [ ] Confirm spend order (DIEM, then earned credits, then bundled credits, then USD) if the README mentions it.
- [ ] Confirm the API host is still `https://api.venice.ai/api/v1`. Surplus rejects base URLs off its provider allowlist.
- [ ] If Venice renames model ids, discovery output changes; offers do not migrate themselves. Sellers reprice or relist.

## Release check

- [ ] `pytest` from this repo (fake HTTP; no live seller key required).
- [ ] `hermes plugins validate . --install-deps` when the Hermes CLI is available. Declared `provides_tools` must match `register()`. No core tool overrides.
- [ ] Bump `version` in `plugin.yaml` and `pyproject.toml` together.
- [ ] Secrets stay on `get_secret` only. The SIWE private key stays inside the one-shot signing closure.
- [ ] Catalog pin is a separate PR to `NousResearch/hermes-agent` `plugin-catalog/surplus-intelligence-seller.yaml`. Paste `docs/catalog-entry.example.yaml`, replace the SHA placeholder with the full 40-hex commit, and only open that PR with write access and intent to submit. Do not point the pin at a branch.
