# Surplus Intelligence seller (Venice DIEM)

**One goal: earn the maximum possible every day by selling the entire Venice DIEM allowance before it expires.**

[DIEM](https://docs.venice.ai/overview/vvv-diem) staked on Venice is about **$1 of inference credit per DIEM per epoch**. The allowance resets at **00:00 UTC**. Unused credit **does not roll over**, so DIEM left at the reset earns nothing. This plugin lists that capacity on [Surplus Intelligence](https://www.surplusintelligence.ai/) and keeps the offers cheap enough, capped high enough, and spread across enough Venice models that buyers can clear the day.

Surplus routes buyers to your offers. Earnings settle in **USDC on Base** to your payout wallet.

Seller-only and Venice-only:

- Provider base URL is always `https://api.venice.ai/api/v1`.
- No inference shopping, no buyer balance top-up, no other providers.
- Text offers use `cost_multiplier` or a discount. Per-token prices are retired.
- Automatic repricing only gets **cheaper**. It never raises your price in the middle of an epoch.

## Daily loop

Run this several times each UTC day, and again in the last few hours:

1. `surplus_diem_capacity` — DIEM left, epoch allocation, hours to reset, offer caps, `leftover_diem_risk`.
2. `surplus_rebalance_diem` with `apply=true` — deepen the discount if sell-through is behind the clock, raise any `cap_daily_usd` that sits under the epoch budget, pause unhealthy offers.
3. `surplus_fill_remaining_diem` when risk is `watch`, `high`, or `critical` (it defaults to `apply=true`). In the last hour with DIEM still unsold the price goes to 75% off the reference.
4. `surplus_diem_utilization` — consumed DIEM versus the allocation, plus today's Surplus earnings. Confirm the leftover is heading to ~$0.

`/surplus-seller` prints the same risk line without secret values.

## DIEM → Venice key → Surplus offer

1. Stake DIEM and create a Venice API key (`VENICE_API_KEY`) dedicated to this seller, so the balance the plugin reads is the balance it is trying to sell. Set that key's USD consumption limit to 0 if the account also holds paid credit (see [paid credit](#if-the-venice-account-also-holds-paid-credit)). Do **not** set a Venice epoch consumption limit below the staked DIEM. That limit would strand allowance the same way a low Surplus cap would.
2. Optional: a Venice **admin** key (`VENICE_ADMIN_API_KEY`) on the same account. `GET /billing/balance` returns remaining DIEM and `diemEpochAllocation`. An inference key often gets 401 there; `GET /api_keys/rate_limits` still returns remaining DIEM and `nextEpochBegins`.
3. Choose the Base wallet that receives USDC (`SURPLUS_SELLER_PAYOUT_ADDRESS`).
4. Mint a Surplus seller key (`si_seller_…`) with `surplus_seller_auth`, or paste one you already have.
5. `surplus_cover_venice_models` lists the models buyers can actually hit, at the utilization price, each capped at the epoch allocation. One **active** offer is allowed per `(Venice API key, https://api.venice.ai/api/v1, model)`.

Surplus encrypts the Venice key at rest and uses it only to serve buyers and to probe the offer. A second active offer for the same triple returns `409 duplicate_offer_for_credential`.

## Security

Secrets are read with Hermes `agent.secret_scope.get_secret`. This plugin never opens a `.env` file, `auth.json`, or another tool's credential store.

| Secret | Role |
| --- | --- |
| `VENICE_API_KEY` | Venice key whose DIEM capacity is sold. Required to load the plugin. |
| `SURPLUS_SELLER_PAYOUT_ADDRESS` | `0x…` Base wallet for USDC earnings. Required to load the plugin. |
| `SURPLUS_SELLER_API_KEY` | `si_seller_…` seller key. Optional, because `surplus_seller_auth` can mint one. |
| `VENICE_ADMIN_API_KEY` | Optional. Reads `diemEpochAllocation` from `GET /billing/balance`. |

`SURPLUS_SELLER_API_KEY` is declared as `optional_env`, not `requires_env`. A required seller key would disable the plugin before the mint tool can run. After a mint, paste the key into Hermes secrets. The in-memory copy is dropped on restart.

Minting uses a **throwaway wallet**:

- The plugin asks Surplus for a SIWE challenge, signs it, and exchanges the signature for an `si_seller_…` key.
- The private key lives only inside the signing closure for that one request. It is not logged, not returned, and not written anywhere.
- That wallet holds no funds. Do not send VVV, DIEM, or USDC to it. Earnings settle only to `SURPLUS_SELLER_PAYOUT_ADDRESS`.
- A second SIWE mint would be a **different seller identity**. If a key is already set, the tool refuses unless you pass `issue_additional=true`, which calls `POST /v1/seller/keys` on the same account.

The issued seller key is shown once and is then in the chat transcript. Save it and revoke it if the chat is shared. A seller key can call Surplus payout endpoints, including a destination override. **This plugin never withdraws or redirects payouts.** Treat `si_seller_…` like a treasury credential.

Deleting an offer is a soft delete. Revoke the Venice key in Venice if you need the upstream credential invalidated immediately.

## Setup

Install the plugin from a checkout of this repo (catalog install is a separate step below):

```bash
hermes plugins install /path/to/surplus-intelligence-seller
# or, once a catalog entry exists:
# hermes plugins install surplus-intelligence-seller
```

Hermes prompts for `requires_env` values during install. Set the optional seller key the same way, or mint it from chat:

- `VENICE_API_KEY` — https://venice.ai/settings/api
- `SURPLUS_SELLER_PAYOUT_ADDRESS` — your Base wallet
- `SURPLUS_SELLER_API_KEY` — leave empty to mint, or paste an existing `si_seller_…` key
- `VENICE_ADMIN_API_KEY` — optional admin key so sell-through can be scored against the full epoch allocation

Python dependency (installed with the plugin): `eth-account>=0.13.7,<0.15`, used only to sign the SIWE challenge.

## Tools

Handlers return JSON strings and accept `**kwargs`.

| Tool | What it does |
| --- | --- |
| `surplus_seller_auth` | SIWE-mint an `si_seller_…` key, or issue another key on the same account. |
| `surplus_list_offers` | List your offers. `paused` is queried as `inactive` (that is how Surplus stores a pause). |
| `surplus_create_offer` | List one Venice model. Omitted price uses the utilization schedule. Omitted `cap_daily_usd` uses the DIEM epoch allocation when known. |
| `surplus_update_offer` | Reprice or change the daily cap (`PATCH`). |
| `surplus_pause_or_cancel_offer` | `pause` (`PATCH status=paused`) or `cancel` (`DELETE`, soft delete). |
| `surplus_resume_offer` | `PATCH status=active`. Fails if the triple is taken or the offer was operator-delisted. |
| `surplus_seller_earnings` | Earnings for `today`, `7d`, `30d`, `90d`, `mtd`, or `lifetime`, plus the payout position. |
| `surplus_discover_venice_models` | `POST /v1/seller/discover` against Venice. |
| `surplus_test_venice_connection` | `POST /v1/seller/test-connection` (one model) or `test-health` (several). |
| `surplus_diem_capacity` | Venice DIEM left, epoch allocation, hours to reset, offer caps, recommended price, leftover risk. |
| `surplus_diem_utilization` | Consumed DIEM vs allocation, Surplus earnings today, caps that would strand DIEM. |
| `surplus_cover_venice_models` | Bulk-create or resume offers so capacity is not stuck on one model. Sets the payout wallet on every new offer. |
| `surplus_rebalance_diem` | Mid-day plan. `apply=true` deepens discounts, raises caps, pauses unhealthy offers. Default is a dry run. |
| `surplus_fill_remaining_diem` | Late-epoch burn-down. Default `apply=true`. No-op when DIEM remaining is about $0. |

`/surplus-seller` prints seller-key presence, the payout address (shortened), offer lines, and leftover DIEM risk. It does not print secret values.

### Pricing: utilization first

Margin versus a full sell-through is a real tradeoff. A 5% discount earns more per token and can leave DIEM unsold at midnight, which earns $0 on the remainder. This plugin **defaults to utilization**.

| Situation | Discount | `cost_multiplier` |
| --- | --- | --- |
| On pace, early in the epoch | 20% | 0.80 |
| Behind the clock, or a large balance remains after midday | 30% | 0.70 |
| Clearly behind, or a large balance remains with ≤6 hours left | 45% | 0.55 |
| ≤3 hours left and DIEM remains | 60% | 0.40 |
| Last hour and DIEM remains | 75% | 0.25 |

Pass an explicit `cost_multiplier` or `discount_pct` when you want a different price. `discount_pct` 15 is `cost_multiplier` 0.85. The create body sends `pricing_mode: cost_multiplier` and, when the discount is between 0 and 100, `discount` as well.

Every price, including one the model picks, has a hard floor: `cost_multiplier` **0.10** (`discount_pct` at most 90). Anything lower is rejected with `code: cost_multiplier_below_floor`, so the model cannot list your Venice capacity for free or near-free. The floor sits below the automatic 0.25 clearance band, leaving room for a deliberate last-minute fire sale. It is `MIN_COST_MULTIPLIER` in `strategy.py`.

`cap_daily_usd` defaults to the **epoch allocation** (the full day's DIEM budget), not a small stub and not "remaining" alone. Remaining can sit under spend Surplus has already counted and stop routing early. Each covered model gets that same ceiling; on a DIEM-only key Venice itself stops when the DIEM balance hits zero (see paid credit below), so one hot model can still consume the day. A cap below the remaining balance is called out as leftover risk.

#### If the Venice account also holds paid credit

Venice draws on every balance the account has (DIEM, bundled credits, and paid USD credit) automatically; a request does not pick one. The plugin cannot tell Surplus "DIEM only". Each offer's `cap_daily_usd` defaults to the full epoch allocation, and every covered model gets that same cap, so the caps added together can be several times the day's DIEM. If the account holds paid credit, buyer traffic can keep routing after the DIEM is gone and be billed to that **paid USD balance**. Surplus still pays you the offer price, but at a discount that sale earns less than the USD it burns.

To keep sales DIEM-only, use a dedicated Venice key for `VENICE_API_KEY` with **USD spending disabled**:

- Recommended: in [venice.ai/settings/api](https://venice.ai/settings/api) set the key's Epoch Consumption Limit for **USD to 0** (or create it with USD spending disabled; via the API, `consumptionLimit: {"usd": 0}`). Leave the **DIEM** limit empty so the whole allowance stays sellable. Requests past the DIEM then fail with `402 API_KEY_USD_SPEND_LIMIT_EXCEEDED` instead of spending paid credit.
- Or keep that key on an account that holds only staked DIEM and no paid USD balance.
- Or pass explicit `cap_daily_usd` values on `surplus_create_offer` / `surplus_update_offer` so the caps summed across offers stay at or under the day's DIEM. This can leave DIEM unsold, and `surplus_rebalance_diem` / `surplus_fill_remaining_diem` with `apply=true` raise any cap below the epoch budget, so prefer the key limit above.

Seller money fields named `*_usdc` are integer **micro-USD** strings (`"5000000"` is 5 USDC). The earnings tool returns both the raw field and a decimal USD conversion.

Earnings accrue when a response completes. Payouts are batched to the payout wallet (read `threshold_usdc` on the payout payload; the operating default has been 5 USDC or 72 hours). A per-request status of `accrued` does not mean the USDC is unpaid. Confirm payment from payout history entries with `status: paid` and a Base `tx_hash`.

API reference: [seller endpoints](https://www.surplusintelligence.ai/docs/api-reference/seller-endpoints) and the [llms.txt](https://www.surplusintelligence.ai/llms.txt) seller section. Seller quickstart: https://www.surplusintelligence.ai/docs/getting-started/seller-quickstart.

## Catalog submission

Listing this plugin in Hermes is a **separate pull request** to [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) under `plugin-catalog/surplus-intelligence-seller.yaml`. That PR must pin the full 40-character commit SHA of this repo. Do not send that PR unless you have write access and intend to submit it.

A ready-to-paste entry with a SHA placeholder is in [`docs/catalog-entry.example.yaml`](docs/catalog-entry.example.yaml).

Before submitting, from a Hermes checkout:

```bash
hermes plugins validate /path/to/surplus-intelligence-seller --install-deps
```

`provides_tools` matches the tools `register()` registers. The plugin does not override Hermes core tools.

## Development

```bash
git clone https://github.com/ProlowN/surplus-intelligence-seller surplus_intelligence_seller
cd surplus_intelligence_seller   # the root __init__.py makes pytest import this dir as a package, so its name must be a valid identifier
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Tests fake Surplus HTTP. They do not call Venice or Surplus. pytest lives in `requirements-dev.txt`, not in `pyproject.toml`, so installing the plugin into Hermes never pulls a pytest pin that can conflict with Hermes core.
