"""Seller plugin tests. HTTP is faked; no Surplus or Venice calls."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "surplus_intelligence_seller"


def load_plugin():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def plugin():
    module = load_plugin()
    client_mod = importlib.import_module(f"{PACKAGE}.client")
    client_mod.set_client(None)
    yield module, client_mod
    client_mod.set_client(None)


class FakeHTTP:
    def __init__(self, responder):
        self.calls = []
        self._responder = responder

    def __call__(self, request, timeout):
        body = None
        if request.data:
            body = json.loads(request.data.decode("utf-8"))
        headers = {key.lower(): value for key, value in request.header_items()}
        call = {
            "method": request.method,
            "url": request.full_url,
            "headers": headers,
            "body": body,
            "timeout": timeout,
        }
        self.calls.append(call)
        status, payload = self._responder(call)
        encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return status, {"content-type": "application/json"}, encoded


def use_client(client_mod, responder, secrets=None):
    http = FakeHTTP(responder)
    values = secrets or {}

    def lookup(name):
        return values.get(name)

    client = client_mod.SurplusClient(transport=http, secret_lookup=lookup)
    client_mod.set_client(client)
    return client, http


def test_manifest_tools_match_register(plugin):
    module, _client_mod = plugin
    text = (ROOT / "plugin.yaml").read_text()
    names = []
    capture = False
    for line in text.splitlines():
        if line.startswith("provides_tools:"):
            capture = True
            continue
        if not capture:
            continue
        if line.startswith("  - "):
            names.append(line.split("  - ", 1)[1].strip())
            continue
        if line.startswith("#") or line.strip() == "":
            continue
        break
    assert names == list(module.schemas.SCHEMAS)
    registered = []

    class Ctx:
        def register_tool(self, **kwargs):
            assert kwargs.get("override") is not True
            registered.append(kwargs["name"])
            assert kwargs["schema"]["name"] == kwargs["name"]
            assert kwargs["handler"].__name__ == kwargs["name"]

        def register_command(self, name, handler, description="", args_hint=""):
            assert name == "surplus-seller"
            assert callable(handler)

    module.register(Ctx())
    assert registered == names


def test_source_does_not_read_dotenv():
    for path in ROOT.glob("*.py"):
        text = path.read_text()
        assert "load_dotenv" not in text
        assert "dotenv_values" not in text
        assert "os.environ" not in text


def test_pricing_and_retired_per_token(plugin):
    _module, client_mod = plugin
    body = client_mod.parse_pricing(discount_pct="15%")
    assert body["pricing_mode"] == "cost_multiplier"
    assert body["cost_multiplier"] == pytest.approx(0.85)
    assert body["discount"] == pytest.approx(15)
    with pytest.raises(client_mod.SurplusError):
        client_mod.parse_pricing(cost_multiplier=0.5, discount_pct=15)
    with pytest.raises(client_mod.SurplusError):
        client_mod.parse_pricing()
    assert client_mod.micro_usdc_to_usd("5000000") == "5.000000"


@pytest.mark.parametrize("value", [0, 0.0, "0", 0.01, 0.0999, -0.5])
def test_cost_multiplier_floor_rejects_free_or_near_free(plugin, value):
    _module, client_mod = plugin
    with pytest.raises(client_mod.SurplusError) as excinfo:
        client_mod.parse_pricing(cost_multiplier=value)
    if float(value) >= 0:
        assert excinfo.value.code == "cost_multiplier_below_floor"
        assert "0.1" in excinfo.value.message


@pytest.mark.parametrize("value", [100, "95%", 90.5])
def test_discount_above_floor_is_rejected(plugin, value):
    _module, client_mod = plugin
    with pytest.raises(client_mod.SurplusError) as excinfo:
        client_mod.parse_pricing(discount_pct=value)
    assert excinfo.value.code == "cost_multiplier_below_floor"


def test_cost_multiplier_floor_boundaries(plugin):
    _module, client_mod = plugin
    from surplus_intelligence_seller import strategy

    assert strategy.MIN_COST_MULTIPLIER == pytest.approx(0.10)
    assert strategy.MAX_DISCOUNT_PCT == pytest.approx((1 - strategy.MIN_COST_MULTIPLIER) * 100)
    # The automatic clearance band must stay above the hard floor.
    assert 1 - strategy.CLEARANCE_DISCOUNT_PCT / 100 >= strategy.MIN_COST_MULTIPLIER
    assert client_mod.parse_pricing(cost_multiplier=0.10)["cost_multiplier"] == pytest.approx(0.10)
    assert client_mod.parse_pricing(discount_pct=90)["cost_multiplier"] == pytest.approx(0.10)
    assert client_mod.parse_pricing(cost_multiplier=0.25)["discount"] == pytest.approx(75)
    assert client_mod.parse_pricing(cost_multiplier=2)["cost_multiplier"] == 2
    with pytest.raises(client_mod.SurplusError):
        client_mod.parse_pricing(cost_multiplier=2.01)


def test_update_offer_floor_blocks_before_network(plugin):
    _module, client_mod = plugin
    calls = []

    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("no network call expected")

    client = client_mod.SurplusClient(transport=transport, secret_lookup=lambda name: None)
    with pytest.raises(client_mod.SurplusError) as excinfo:
        client.update_offer(offer_id="off_1", cost_multiplier=0)
    assert excinfo.value.code == "cost_multiplier_below_floor"
    assert calls == []


def test_pyproject_has_no_dev_extra_that_conflicts_with_hermes():
    text = (ROOT / "pyproject.toml").read_text()
    assert "optional-dependencies" not in text
    assert "pytest" not in text.split("[tool.pytest.ini_options]")[0]
    assert "pytest" in (ROOT / "requirements-dev.txt").read_text()


def test_siwe_mint_discards_private_key_and_caches_seller_key(plugin, monkeypatch):
    module, client_mod = plugin
    from eth_account import Account
    from eth_account.messages import encode_defunct

    seen = {}
    original = Account.create

    def spy(*args, **kwargs):
        account = original(*args, **kwargs)
        seen["hex"] = account.key.hex().removeprefix("0x").lower()
        seen["address"] = account.address
        return account

    monkeypatch.setattr(Account, "create", spy)

    def responder(call):
        if call["method"] == "GET":
            assert call["url"].startswith(
                "https://api.surplusintelligence.ai/v1/seller/auth/challenge?address=0x"
            )
            assert "authorization" not in call["headers"]
            return 200, {"message": "www.surplusintelligence.ai wants you to sign in", "nonce": "abc"}
        assert call["url"].endswith("/v1/seller/auth/keys")
        assert "idempotency-key" in call["headers"]
        assert "authorization" not in call["headers"]
        message = call["body"]["message"]
        signature = call["body"]["signature"]
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
        assert recovered == seen["address"]
        assert len(signature) == 132
        return 200, {"key": "si_seller_testkeyvalue123456", "id": "key_1", "wallet": seen["address"]}

    client, http = use_client(client_mod, responder)
    client._wallet_factory = client_mod.default_wallet_factory
    raw = module.tools.surplus_seller_auth({}, task_id="t")
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["key"] == "si_seller_testkeyvalue123456"
    assert payload["private_key_stored"] is False
    assert payload["persisted"] is False
    assert seen["hex"] not in raw.lower()
    assert seen["hex"] not in json.dumps(client.__dict__, default=str).lower()
    assert client._session_seller_key == payload["key"]
    assert len(http.calls) == 2

    again = json.loads(module.tools.surplus_seller_auth({}))
    assert again["already_configured"] is True
    assert "key" not in again
    assert again["key_prefix"].startswith("si_seller_")


def test_create_offer_is_venice_only_with_idempotency(plugin):
    module, client_mod = plugin

    def responder(call):
        assert call["method"] == "POST"
        assert call["url"] == "https://api.surplusintelligence.ai/v1/seller/offers"
        assert call["headers"]["authorization"] == "Bearer si_seller_configuredkey"
        assert call["headers"]["idempotency-key"]
        body = call["body"]
        assert body["seller_base_url"] == "https://api.venice.ai/api/v1"
        assert body["api_key"] == "venice-live-key"
        assert body["payout_address"] == "0x1111111111111111111111111111111111111111"
        assert body["pricing_mode"] == "cost_multiplier"
        assert body["cost_multiplier"] == pytest.approx(0.5)
        assert "price_input_per_1m" not in body
        return 200, {
            "id": "offer_1",
            "model": "llama-3.3-70b",
            "api_key": "venice-live-key",
            "status": "active",
        }

    use_client(
        client_mod,
        responder,
        {
            "SURPLUS_SELLER_API_KEY": "si_seller_configuredkey",
            "VENICE_API_KEY": "venice-live-key",
            "SURPLUS_SELLER_PAYOUT_ADDRESS": "0x1111111111111111111111111111111111111111",
        },
    )
    payload = json.loads(
        module.tools.surplus_create_offer(
            {"model": "llama-3.3-70b", "cost_multiplier": 0.5, "cap_daily_usd": 25}
        )
    )
    assert payload["ok"] is True
    assert payload["seller_base_url"] == "https://api.venice.ai/api/v1"
    assert "api_key" not in payload["offer"]
    assert payload["offer"]["id"] == "offer_1"

    rejected = json.loads(
        module.tools.surplus_create_offer(
            {
                "model": "llama-3.3-70b",
                "cost_multiplier": 0.5,
                "seller_base_url": "https://openrouter.ai/api/v1",
            }
        )
    )
    assert rejected["ok"] is False
    per_token = json.loads(
        module.tools.surplus_create_offer(
            {"model": "llama-3.3-70b", "price_input_per_1m": 1, "price_output_per_1m": 2}
        )
    )
    assert per_token["ok"] is False
    assert "retired" in per_token["error"]


def test_pause_cancel_resume_and_duplicate(plugin):
    module, client_mod = plugin
    seen = []

    def responder(call):
        seen.append((call["method"], call["url"], call["body"], "idempotency-key" in call["headers"]))
        if call["method"] == "DELETE":
            return 204, b""
        if "duplicate" in call["url"]:
            return 409, {
                "error": {
                    "message": "Already listed",
                    "code": "duplicate_offer_for_credential",
                    "detail": {"existing_offer_id": "offer_existing"},
                }
            }
        return 200, {"id": "offer_9", "status": (call["body"] or {}).get("status", "active")}

    use_client(
        client_mod,
        responder,
        {"SURPLUS_SELLER_API_KEY": "si_seller_configuredkey"},
    )
    paused = json.loads(
        module.tools.surplus_pause_or_cancel_offer({"offer_id": "offer_9", "action": "pause"})
    )
    assert paused["ok"] is True
    assert seen[0][0] == "PATCH"
    assert seen[0][2] == {"status": "paused"}
    assert seen[0][3] is False

    cancelled = json.loads(
        module.tools.surplus_pause_or_cancel_offer({"offer_id": "offer_9", "action": "cancel"})
    )
    assert cancelled["action"] == "cancel"
    assert seen[1][0] == "DELETE"

    resumed = json.loads(module.tools.surplus_resume_offer({"offer_id": "offer_9"}))
    assert resumed["ok"] is True
    assert seen[2][2] == {"status": "active"}

    def duplicate(call):
        return 409, {
            "error": {
                "message": "Already listed",
                "type": "invalid_request_error",
                "code": "duplicate_offer_for_credential",
                "detail": {"existing_offer_id": "offer_existing"},
            }
        }

    client_mod.get_client()._transport = FakeHTTP(duplicate)
    missing = json.loads(module.tools.surplus_resume_offer({"offer_id": "duplicate"}))
    assert missing["ok"] is False
    assert missing["status"] == 409
    assert "offer_existing" in missing["error"]


def test_list_discover_earnings_and_probe(plugin):
    module, client_mod = plugin

    def responder(call):
        if call["url"].startswith("https://api.surplusintelligence.ai/v1/seller/offers"):
            assert "status=inactive" in call["url"]
            assert "idempotency-key" not in call["headers"]
            return 200, {
                "items": [
                    {"id": "offer_1", "model": "llama-3.3-70b", "status": "inactive", "api_key": "venice-live-key"}
                ]
            }
        if "/v1/seller/discover" in call["url"]:
            assert call["body"]["base_url"] == "https://api.venice.ai/api/v1"
            assert call["body"]["api_key"] == "venice-live-key"
            ndjson = "\n".join(
                [
                    json.dumps({"model": "llama-3.3-70b", "supported": True, "pricing": {"input_per_1m": 0.2}}),
                    json.dumps({"model": "hidden", "supported": False}),
                ]
            )
            return 200, ndjson.encode("utf-8")
        if "/v1/seller/earnings" in call["url"]:
            assert "range=7d" in call["url"]
            return 200, {"total_earned_usdc": "2500000", "pending_usdc": "0", "by_model": [], "recent_sales": []}
        if "/v1/seller/payouts" in call["url"]:
            return 200, {
                "available_usdc": "5000000",
                "paid_usdc": "1000000",
                "threshold_usdc": "5000000",
                "mode": "batched",
                "history": [{"status": "paid", "amount_usdc": "1000000", "tx_hash": "0xabc"}],
            }
        if "/v1/seller/test-connection" in call["url"]:
            assert call["body"]["model"] == "llama-3.3-70b"
            assert call["body"]["base_url"] == "https://api.venice.ai/api/v1"
            return 200, {"ok": True, "latency_ms": 120}
        raise AssertionError(call["url"])

    use_client(
        client_mod,
        responder,
        {
            "SURPLUS_SELLER_API_KEY": "si_seller_configuredkey",
            "VENICE_API_KEY": "venice-live-key",
        },
    )
    listed = json.loads(module.tools.surplus_list_offers({"status": "paused"}))
    assert listed["count"] == 1
    assert "api_key" not in listed["offers"][0]
    assert "inactive" in listed["status_note"]

    found = json.loads(module.tools.surplus_discover_venice_models({"limit": 10}))
    assert found["supported"] == 1
    assert found["models"][0]["model"] == "llama-3.3-70b"

    money = json.loads(module.tools.surplus_seller_earnings({}))
    assert money["earnings"]["usd"]["total_earned_usd"] == "2.500000"
    assert money["payouts"]["usd"]["available_usd"] == "5.000000"
    assert money["payouts"]["history"][0]["tx_hash"] == "0xabc"

    probe = json.loads(module.tools.surplus_test_venice_connection({"model": "llama-3.3-70b"}))
    assert probe["ok"] is True
    assert probe["base_url"] == "https://api.venice.ai/api/v1"


def test_slash_command_hides_secrets(plugin):
    module, client_mod = plugin
    use_client(
        client_mod,
        lambda call: (200, {"items": []}),
        {
            "SURPLUS_SELLER_API_KEY": "si_seller_configuredkey",
            "VENICE_API_KEY": "venice-live-key",
            "SURPLUS_SELLER_PAYOUT_ADDRESS": "0x2222222222222222222222222222222222222222",
        },
    )
    text = module.tools.surplus_seller_command("status", platform="cli")
    assert "venice-live-key" not in text
    assert "si_seller_configuredkey" not in text
    assert "0x2222" in text
    assert "Venice only" in text
    assert "00:00 UTC" in text
    assert "help" in module.tools.surplus_seller_command("help").lower() or "Tools:" in module.tools.surplus_seller_command(
        "help"
    )


LATE = datetime(2026, 10, 26, 22, 30, tzinfo=timezone.utc)
SELLER_SECRETS = {
    "SURPLUS_SELLER_API_KEY": "si_seller_configuredkey",
    "VENICE_API_KEY": "venice-live-key",
    "SURPLUS_SELLER_PAYOUT_ADDRESS": "0x1111111111111111111111111111111111111111",
}
VENICE_BASE = "https://api.venice.ai/api/v1"


def _freeze(client, moment):
    client._clock = lambda: moment


def _billing(remaining=80, allocation=100):
    return {
        "canConsume": True,
        "consumptionCurrency": "DIEM",
        "balances": {"diem": remaining},
        "diemEpochAllocation": allocation,
    }


def _rate_limits(remaining=80):
    return {
        "data": {
            "balances": {"DIEM": remaining},
            "nextEpochBegins": "2026-10-27T00:00:00Z",
        }
    }


def test_discount_bands_and_cap_prefer_allocation(plugin):
    strategy = importlib.import_module(f"{PACKAGE}.strategy")

    opening = strategy.recommend_discount(
        hours_left=22, sell_through=0.8, remaining_usd=20, allocation_usd=100
    )
    assert opening["discount_pct"] == 20
    assert opening["cost_multiplier"] == pytest.approx(0.8)

    behind = strategy.recommend_discount(
        hours_left=10, sell_through=0.5, remaining_usd=50, allocation_usd=100
    )
    assert behind["discount_pct"] == 30
    assert behind["cost_multiplier"] == pytest.approx(0.7)

    late = strategy.recommend_discount(
        hours_left=5, sell_through=0.4, remaining_usd=60, allocation_usd=100
    )
    assert late["discount_pct"] == 45
    assert late["cost_multiplier"] == pytest.approx(0.55)

    urgent = strategy.recommend_discount(
        hours_left=1.5, sell_through=0.2, remaining_usd=80, allocation_usd=100
    )
    assert urgent["discount_pct"] == 60
    assert urgent["cost_multiplier"] == pytest.approx(0.4)

    clearance = strategy.recommend_discount(
        hours_left=0.5, sell_through=0.2, remaining_usd=80, allocation_usd=100
    )
    assert clearance["discount_pct"] == 75
    assert clearance["cost_multiplier"] == pytest.approx(0.25)

    assert strategy.leftover_risk(hours_left=1.5, remaining_usd=80, allocation_usd=100) == "critical"
    assert strategy.leftover_risk(hours_left=2, remaining_usd=0, allocation_usd=100) == "none"
    assert strategy.suggested_daily_cap(100, 12.5) == 100
    assert strategy.suggested_daily_cap(None, 12.5) == 12.5

    clock = strategy.epoch_clock(LATE, "2026-10-27T00:00:00Z")
    assert clock["hours_left"] == pytest.approx(1.5)

    sold = strategy.plan_sellthrough(
        capacity={"diem_remaining_usd": 0, "recommendation": urgent, "suggested_cap_daily_usd": 100},
        offers=[],
        candidate_models=["llama-3.3-70b"],
        hot_models=[],
        max_new=4,
        pause_unhealthy=True,
        pause_cold=False,
    )
    assert sold["actions"] == []

    deepen = strategy.plan_sellthrough(
        capacity={
            "diem_remaining_usd": 80,
            "recommendation": urgent,
            "suggested_cap_daily_usd": 100,
            "leftover_diem_risk": "critical",
        },
        offers=[
            {
                "id": "offer_hot",
                "model": "llama-3.3-70b",
                "status": "active",
                "healthy": True,
                "cost_multiplier": 0.85,
                "cap_daily_usd": 10,
            }
        ],
        candidate_models=[],
        hot_models=["llama-3.3-70b"],
        max_new=0,
        pause_unhealthy=True,
        pause_cold=False,
    )
    assert len(deepen["actions"]) == 1
    action = deepen["actions"][0]
    assert action["action"] == "update"
    assert action["cost_multiplier"] == pytest.approx(0.4)
    assert action["cap_daily_usd"] == 100

    held = strategy.plan_sellthrough(
        capacity={
            "diem_remaining_usd": 80,
            "recommendation": opening,
            "suggested_cap_daily_usd": 100,
        },
        offers=[
            {
                "id": "offer_cheap",
                "model": "llama-3.3-70b",
                "status": "active",
                "healthy": True,
                "cost_multiplier": 0.25,
                "cap_daily_usd": 100,
            }
        ],
        candidate_models=[],
        hot_models=["llama-3.3-70b"],
        max_new=0,
        pause_unhealthy=True,
        pause_cold=False,
    )
    assert held["actions"] == []


def _offer_rows():
    return {
        "items": [
            {
                "id": "offer_hot",
                "model": "llama-3.3-70b",
                "status": "active",
                "healthy": True,
                "cost_multiplier": 0.85,
                "cap_daily_usd": 10,
            },
            {
                "id": "offer_cold",
                "model": "venice-cold",
                "status": "active",
                "healthy": True,
                "cost_multiplier": 0.85,
                "cap_daily_usd": 10,
            },
            {
                "id": "offer_bad",
                "model": "venice-bad",
                "status": "active",
                "healthy": False,
                "cost_multiplier": 0.85,
                "cap_daily_usd": 10,
            },
        ]
    }


def test_capacity_falls_back_when_billing_is_unauthorized(plugin):
    module, client_mod = plugin

    def responder(call):
        if "billing/balance" in call["url"]:
            assert call["headers"]["authorization"] == "Bearer venice-live-key"
            return 401, {"error": {"message": "admin key required"}}
        if "rate_limits" in call["url"]:
            assert call["headers"]["authorization"] == "Bearer venice-live-key"
            return 200, {"data": {"balances": {"DIEM": 42.5}, "nextEpochBegins": "2026-10-27T00:00:00Z"}}
        if call["method"] == "GET" and "/v1/seller/offers" in call["url"]:
            return 200, {"items": [{"id": "o1", "model": "llama-3.3-70b", "status": "active", "cap_daily_usd": 1}]}
        raise AssertionError(call["url"])

    _client, http = use_client(
        client_mod,
        responder,
        {"SURPLUS_SELLER_API_KEY": "si_seller_configuredkey", "VENICE_API_KEY": "venice-live-key"},
    )
    _freeze(_client, datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc))
    payload = json.loads(module.tools.surplus_diem_capacity({}))
    assert payload["ok"] is True
    assert payload["diem_remaining_usd"] == pytest.approx(42.5)
    assert payload["diem_epoch_allocation_usd"] is None
    assert payload["suggested_cap_daily_usd"] == pytest.approx(42.5)
    assert payload["sources"] == ["api_keys/rate_limits"]
    assert payload["leftover_diem_risk"] == "watch"
    assert payload["offers"][0]["cap_below_diem_budget"] is True
    assert any("admin key required" in note for note in payload["notes"])
    assert "venice-live-key" not in json.dumps(payload)
    assert any("billing/balance" in call["url"] for call in http.calls)


def test_capacity_uses_admin_allocation(plugin):
    module, client_mod = plugin
    billing_auth = []

    def responder(call):
        if "billing/balance" in call["url"]:
            billing_auth.append(call["headers"]["authorization"])
            return 200, _billing(80, 100)
        if "rate_limits" in call["url"]:
            assert call["headers"]["authorization"] == "Bearer venice-live-key"
            return 200, _rate_limits(80)
        if "/v1/seller/offers" in call["url"]:
            return 200, {"items": []}
        raise AssertionError(call["url"])

    client, _http = use_client(
        client_mod,
        responder,
        {**SELLER_SECRETS, "VENICE_ADMIN_API_KEY": "venice-admin-key"},
    )
    _freeze(client, LATE)
    payload = json.loads(module.tools.surplus_diem_capacity({}))
    assert billing_auth == ["Bearer venice-admin-key"]
    assert payload["diem_epoch_allocation_usd"] == pytest.approx(100)
    assert payload["diem_consumed_usd"] == pytest.approx(20)
    assert payload["sell_through"] == pytest.approx(0.2)
    assert payload["suggested_cap_daily_usd"] == pytest.approx(100)
    assert payload["leftover_diem_risk"] == "critical"
    assert payload["recommendation"]["discount_pct"] == 60
    assert "venice-admin-key" not in json.dumps(payload)


def test_create_offer_defaults_to_utilization_price_and_epoch_cap(plugin):
    module, client_mod = plugin
    seen = {}

    def responder(call):
        if "billing/balance" in call["url"]:
            return 200, _billing()
        if "rate_limits" in call["url"]:
            return 200, _rate_limits()
        assert call["method"] == "POST"
        assert call["url"].endswith("/v1/seller/offers")
        assert "idempotency-key" in call["headers"]
        seen["body"] = call["body"]
        return 200, {"id": "offer_new", "model": "llama-3.3-70b", "status": "active"}

    client, _http = use_client(client_mod, responder, SELLER_SECRETS)
    _freeze(client, LATE)
    payload = json.loads(module.tools.surplus_create_offer({"model": "llama-3.3-70b"}))
    assert payload["ok"] is True
    body = seen["body"]
    assert body["seller_base_url"] == VENICE_BASE
    assert body["api_key"] == "venice-live-key"
    assert body["cost_multiplier"] == pytest.approx(0.4)
    assert body["discount"] == pytest.approx(60)
    assert body["cap_daily_usd"] == pytest.approx(100)
    assert "api_key" not in payload["offer"]
    assert "venice-live-key" not in json.dumps(payload)


def test_rebalance_dry_run_does_not_write_offers(plugin):
    module, client_mod = plugin

    def responder(call):
        if "billing/balance" in call["url"]:
            return 200, _billing()
        if "rate_limits" in call["url"]:
            return 200, _rate_limits()
        if call["method"] == "GET" and "/v1/seller/offers" in call["url"]:
            return 200, _offer_rows()
        if "/v1/seller/earnings" in call["url"]:
            return 200, {
                "total_earned_usdc": "1500000",
                "by_model": [{"model": "llama-3.3-70b", "earned_usd": 1.5}],
            }
        if "/v1/seller/discover" in call["url"]:
            assert call["body"]["base_url"] == VENICE_BASE
            return 200, (json.dumps({"model": "qwen-2.5", "supported": True}) + "\n").encode()
        raise AssertionError((call["method"], call["url"]))

    client, http = use_client(client_mod, responder, SELLER_SECRETS)
    _freeze(client, LATE)
    payload = json.loads(module.tools.surplus_rebalance_diem({"apply": False}))
    assert payload["ok"] is True
    assert payload["applied"] is False
    kinds = [item["action"] for item in payload["actions"]]
    assert kinds.count("update") == 2
    assert kinds.count("pause") == 1
    assert kinds.count("create") == 1
    assert payload["actions"][-1]["model"] == "qwen-2.5"
    assert payload["leftover_diem_risk"] == "critical"
    assert not any(call["method"] in {"PATCH", "DELETE"} for call in http.calls)
    assert not any(call["method"] == "POST" and "/offers" in call["url"] for call in http.calls)


def test_rebalance_apply_reprices_raises_caps_and_pauses(plugin):
    module, client_mod = plugin
    writes = []

    def responder(call):
        if "billing/balance" in call["url"]:
            return 200, _billing()
        if "rate_limits" in call["url"]:
            return 200, _rate_limits()
        if call["method"] == "GET" and call["url"].rstrip("/").endswith("/v1/seller/offers"):
            return 200, _offer_rows()
        if "/v1/seller/earnings" in call["url"]:
            return 200, {"by_model": [{"model": "llama-3.3-70b", "earned_usd": 2}]}
        if "/v1/seller/discover" in call["url"]:
            return 200, (json.dumps({"model": "qwen-2.5", "supported": True}) + "\n").encode()
        if "/offers/bulk" in call["url"]:
            assert call["body"]["base_url"] == VENICE_BASE
            assert call["body"]["offers"][0]["model"] == "qwen-2.5"
            assert call["body"]["offers"][0]["cost_multiplier"] == pytest.approx(0.4)
            assert call["body"]["offers"][0]["cap_daily_usd"] == pytest.approx(100)
            writes.append(("bulk", call["body"]))
            return 200, (json.dumps({"model": "qwen-2.5", "offer_id": "offer_new", "status": "created"}) + "\n").encode()
        if call["method"] == "PATCH":
            writes.append(("patch", call["url"], call["body"]))
            return 200, {"id": "patched", "status": "active"}
        if call["method"] == "DELETE":
            writes.append(("delete", call["url"]))
            return 204, b""
        raise AssertionError((call["method"], call["url"]))

    client, _http = use_client(client_mod, responder, SELLER_SECRETS)
    _freeze(client, LATE)
    payload = json.loads(module.tools.surplus_rebalance_diem({"apply": "true", "max_models": 4}))
    assert payload["applied"] is True
    assert all(row["ok"] for row in payload["results"])
    price_patches = [item for item in writes if item[0] == "patch" and "cost_multiplier" in item[2]]
    assert len(price_patches) == 2
    for _kind, url, body in price_patches:
        assert body["cost_multiplier"] == pytest.approx(0.4)
        assert body["discount"] == pytest.approx(60)
        assert body["cap_daily_usd"] == pytest.approx(100)
        assert "offer_bad" not in url
    pause = [item for item in writes if item[0] == "patch" and item[2] == {"status": "paused"}]
    assert len(pause) == 1
    assert pause[0][1].endswith("/offer_bad")
    payout = [item for item in writes if item[0] == "patch" and item[2].get("payout_address")]
    assert payout[0][2]["payout_address"].startswith("0x1111")
    assert writes[0][0] == "patch"


def test_fill_remaining_widens_and_pauses_cold_models(plugin):
    module, client_mod = plugin
    writes = []

    def responder(call):
        if "billing/balance" in call["url"]:
            return 200, _billing()
        if "rate_limits" in call["url"]:
            return 200, _rate_limits()
        if call["method"] == "GET" and call["url"].rstrip("/").endswith("/v1/seller/offers"):
            return 200, _offer_rows()
        if "/v1/seller/earnings" in call["url"]:
            return 200, {"by_model": [{"model": "llama-3.3-70b", "earned_usd": 2}]}
        if "/v1/seller/discover" in call["url"]:
            line = "\n".join(
                json.dumps({"model": name, "supported": True}) for name in ("qwen-2.5", "llama-3.3-70b")
            )
            return 200, (line + "\n").encode()
        if "/offers/bulk" in call["url"]:
            writes.append(call["body"])
            model = call["body"]["offers"][0]["model"]
            return 200, (json.dumps({"model": model, "offer_id": "offer_new", "status": "created"}) + "\n").encode()
        if call["method"] == "PATCH":
            writes.append((call["url"], call["body"]))
            return 200, {"ok": True}
        raise AssertionError((call["method"], call["url"]))

    client, _http = use_client(client_mod, responder, SELLER_SECRETS)
    _freeze(client, LATE)
    payload = json.loads(module.tools.surplus_fill_remaining_diem({}))
    assert payload["applied"] is True
    assert payload["leftover_diem_risk"] == "critical"
    paused = [item for item in writes if isinstance(item, tuple) and item[1] == {"status": "paused"}]
    assert any(url.endswith("/offer_bad") for url, _body in paused)
    assert any(url.endswith("/offer_cold") for url, _body in paused)
    bulks = [item for item in writes if isinstance(item, dict)]
    assert bulks[0]["offers"][0]["model"] == "qwen-2.5"
    assert bulks[0]["offers"][0]["discount"] == pytest.approx(60)
    assert bulks[0]["base_url"] == VENICE_BASE
    assert "venice-live-key" not in json.dumps(payload)


def test_fill_is_a_noop_when_diem_is_sold_through(plugin):
    module, client_mod = plugin

    def responder(call):
        if "billing/balance" in call["url"]:
            return 200, _billing(remaining=0, allocation=100)
        if "rate_limits" in call["url"]:
            return 200, _rate_limits(0)
        if call["method"] == "GET" and "/v1/seller/offers" in call["url"]:
            return 200, {"items": []}
        if "/v1/seller/earnings" in call["url"]:
            return 200, {"by_model": []}
        if "/v1/seller/discover" in call["url"]:
            return 200, (json.dumps({"model": "qwen-2.5", "supported": True}) + "\n").encode()
        raise AssertionError((call["method"], call["url"]))

    client, http = use_client(client_mod, responder, SELLER_SECRETS)
    _freeze(client, LATE)
    payload = json.loads(module.tools.surplus_fill_remaining_diem({}))
    assert payload["applied"] is True
    assert payload["actions"] == []
    assert "sold through" in payload["summary"]
    assert not any(call["method"] == "POST" and "offers" in call["url"] for call in http.calls)


def test_bulk_create_cancels_when_payout_patch_fails(plugin):
    _module, client_mod = plugin

    def responder(call):
        if "/offers/bulk" in call["url"]:
            assert call["body"]["base_url"] == VENICE_BASE
            assert call["body"]["api_key"] == "venice-live-key"
            return 200, (
                json.dumps({"model": "qwen-2.5", "offer_id": "offer_new", "status": "created"}) + "\n"
            ).encode()
        if call["method"] == "PATCH":
            assert call["body"] == {"payout_address": SELLER_SECRETS["SURPLUS_SELLER_PAYOUT_ADDRESS"]}
            return 400, {"error": {"message": "payout rejected"}}
        if call["method"] == "DELETE":
            assert call["url"].endswith("/v1/seller/offers/offer_new")
            return 204, b""
        raise AssertionError((call["method"], call["url"]))

    client, _http = use_client(client_mod, responder, SELLER_SECRETS)
    rows = client.bulk_create_offers(
        [{"model": "qwen-2.5", "cost_multiplier": 0.4, "discount": 60, "cap_daily_usd": 100}]
    )
    assert rows == [
        {
            "ok": False,
            "model": "qwen-2.5",
            "offer_id": "offer_new",
            "error": "payout rejected",
            "cancelled": True,
            "reason": (
                "Payout address could not be set. The offer was cancelled so earnings "
                "would not settle to the discarded SIWE wallet."
            ),
        }
    ]


def test_utilization_reports_leftover_diem_risk(plugin):
    module, client_mod = plugin

    def responder(call):
        if "billing/balance" in call["url"]:
            return 200, _billing()
        if "rate_limits" in call["url"]:
            return 200, _rate_limits()
        if "/v1/seller/earnings" in call["url"]:
            assert "range=today" in call["url"]
            return 200, {"total_earned_usdc": "2500000", "pending_usdc": "0", "by_model": []}
        if "/v1/seller/payouts" in call["url"]:
            return 200, {"available_usdc": "0", "paid_usdc": "0", "threshold_usdc": "5000000"}
        if "/v1/seller/offers" in call["url"]:
            return 200, {
                "items": [
                    {
                        "id": "offer_hot",
                        "model": "llama-3.3-70b",
                        "status": "active",
                        "cap_daily_usd": 10,
                        "cost_multiplier": 0.85,
                    }
                ]
            }
        raise AssertionError(call["url"])

    client, _http = use_client(client_mod, responder, SELLER_SECRETS)
    _freeze(client, LATE)
    payload = json.loads(module.tools.surplus_diem_utilization({}))
    assert payload["ok"] is True
    assert payload["diem_remaining_usd"] == pytest.approx(80)
    assert payload["diem_consumed_usd"] == pytest.approx(20)
    assert payload["sell_through"] == pytest.approx(0.2)
    assert payload["leftover_diem_risk"] == "critical"
    assert payload["surplus_earned_today_usd"] == "2.500000"
    assert payload["low_caps"][0]["offer_id"] == "offer_hot"
    assert payload["low_caps"][0]["suggested_cap_daily_usd"] == pytest.approx(100)
    assert payload["active_offer_count"] == 1
    assert "venice-live-key" not in json.dumps(payload)
