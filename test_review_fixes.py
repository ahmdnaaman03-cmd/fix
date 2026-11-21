import hmac
import json
import hashlib
import sqlite3
import urllib.parse
import pytest
import app as fixer_app
import utils as fixer_utils
import oauth as fixer_oauth
import billing as fixer_billing
import webhooks as fixer_webhooks
import security as fixer_security

@pytest.fixture
def client(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_review_fixer.db")
    monkeypatch.setattr("db.DB_PATH", test_db)
    monkeypatch.setattr("oauth.DB_PATH", test_db)
    monkeypatch.setattr("routes.DB_PATH", test_db)
    monkeypatch.setattr("webhooks.DB_PATH", test_db)
    monkeypatch.setattr("billing.DB_PATH", test_db)
    monkeypatch.setattr("app.DB_PATH", test_db)

    application = fixer_app.create_app()
    application.config["TESTING"] = True
    with application.test_client() as client:
        yield client

def _insert_store(db_path, fixer_id, store_id, token="shpat_valid_test_token_12345", plan="pro", sub_status="active", sub_id="gid://shopify/AppSubscription/777", orders_count=0):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO stores (fixer_id, store_id, access_token, plan, subscription_status, subscription_id, orders_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fixer_id, store_id, token, plan, sub_status, sub_id, orders_count)
        )
        conn.commit()

def test_downgrade_from_pro_to_free_cancels_shopify_subscription(client, monkeypatch):
    shop = "plan-change-shop.myshopify.com"
    fixer_id = "fx_plan1"
    _insert_store(fixer_billing.DB_PATH, fixer_id, shop)

    state = {"active": True, "cancelled_ids": []}

    def mock_fetch_subs(s, t):
        if state["active"]:
            return [{"id": "gid://shopify/AppSubscription/777", "name": "Fixer Pro Plan", "status": "ACTIVE"}]
        return []

    def mock_cancel_sub(s, t, sid):
        state["cancelled_ids"].append(sid)
        state["active"] = False
        return True

    monkeypatch.setattr(fixer_oauth, "fetch_active_subscriptions_graphql", mock_fetch_subs)
    monkeypatch.setattr(fixer_billing, "fetch_active_subscriptions_graphql", mock_fetch_subs)
    monkeypatch.setattr(fixer_billing, "cancel_shopify_subscription", mock_cancel_sub)

    dash_pro = client.get(f"/api/fixer/dashboard?shop={shop}&fixer_id={fixer_id}")
    assert dash_pro.status_code == 200
    html_pro = dash_pro.get_data(as_text=True)
    assert "Plan: PRO" in html_pro
    assert "Downgrade to Free" in html_pro
    assert "Upgrade Now" not in html_pro

    bad_resp = client.post("/api/fixer/billing/downgrade", data={
        "shop": shop,
        "fixer_id": fixer_id,
        "csrf_token": "invalid-token"
    })
    assert bad_resp.status_code == 403

    csrf = fixer_security.generate_csrf_token(shop, fixer_id)
    down_resp = client.post("/api/fixer/billing/downgrade", data={
        "shop": shop,
        "fixer_id": fixer_id,
        "csrf_token": csrf
    }, follow_redirects=True)
    assert down_resp.status_code == 200
    assert state["cancelled_ids"] == ["gid://shopify/AppSubscription/777"]

    html_free = down_resp.get_data(as_text=True)
    assert "Plan: FREE" in html_free
    assert "Plan Downgraded to Free" in html_free
    assert "Upgrade Now ($4.99/mo)" in html_free

    with sqlite3.connect(fixer_billing.DB_PATH) as conn:
        row = conn.execute("SELECT plan, subscription_status, subscription_id FROM stores WHERE store_id = ?", (shop,)).fetchone()
    assert row == ("free", "downgraded", None)

def test_dashboard_auto_syncs_recent_orders_and_is_idempotent(client, monkeypatch):
    shop = "sync-order-shop.myshopify.com"
    fixer_id = "fx_sync1"
    _insert_store(fixer_billing.DB_PATH, fixer_id, shop, plan="pro", sub_status="active", orders_count=0)

    monkeypatch.setattr(
        fixer_oauth,
        "fetch_active_subscriptions_graphql",
        lambda s, t: [{"id": "gid://shopify/AppSubscription/888", "name": "Fixer Pro Plan", "status": "ACTIVE"}]
    )

    sample_orders = [
        {
            "id": "gid://shopify/Order/9001001",
            "legacyResourceId": "9001001",
            "name": "#1001",
            "phone": None,
            "tags": [],
            "shippingAddress": {
                "firstName": "Ahmed",
                "city": "Cairo",
                "countryCodeV2": "EG",
                "phone": "٠١٠١٢٣٤٥٦٧٨"
            },
            "billingAddress": None,
            "customer": None
        }
    ]
    monkeypatch.setattr(fixer_webhooks, "fetch_recent_orders_graphql", lambda s, t, limit=15: sample_orders)

    updated_inputs = []
    def mock_update(s, t, order_input):
        updated_inputs.append(order_input)
        return True
    monkeypatch.setattr(fixer_webhooks, "execute_order_update_graphql", mock_update)

    resp1 = client.get(f"/api/fixer/dashboard?shop={shop}&fixer_id={fixer_id}")
    assert resp1.status_code == 200
    html1 = resp1.get_data(as_text=True)
    assert "#1001" in html1
    assert "+201012345678" in html1
    assert "Fixer: E.164 Verified" in html1
    assert "Plan: PRO (1 / Unlimited orders)" in html1
    assert "No orders processed yet" not in html1
    assert len(updated_inputs) == 1
    assert "Fixer: E.164 Verified" in updated_inputs[0]["tags"]

    resp2 = client.get(f"/api/fixer/dashboard?shop={shop}&fixer_id={fixer_id}")
    assert resp2.status_code == 200
    html2 = resp2.get_data(as_text=True)
    assert "Plan: PRO (1 / Unlimited orders)" in html2
    assert len(updated_inputs) == 1

def test_register_webhook_graphql_uses_uri_for_2026_01(monkeypatch):
    captured = {}

    class DummyResp:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps({
                "data": {
                    "webhookSubscriptionCreate": {
                        "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/123"},
                        "userErrors": []
                    }
                }
            }).encode("utf-8")

    def fake_urlopen(req, timeout=10):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return DummyResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ok = fixer_oauth.register_webhook_graphql("demo.myshopify.com", "shpat_test_123456789", "ORDERS_CREATE", "/api/fixer/webhook/order")
    assert ok is True
    sub_input = captured["payload"]["variables"]["webhookSubscription"]
    assert "uri" in sub_input
    assert "callbackUrl" not in sub_input

def test_us_phone_number_validated_for_international_reviewers(client):
    resp = client.post("/api/fixer/webhook/order", json={
        "store_id": "us-review-shop.myshopify.com",
        "id": "9002",
        "name": "#1002",
        "shipping_address": {
            "phone": "(555) 234-5678",
            "country_code": "US"
        }
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["clean_phone"] == "+15552345678"
    assert data["validation_status"] == "valid"
    assert data["order_name"] == "#1002"

def test_oauth_callback_succeeds_without_third_party_cookies_in_iframe(client, monkeypatch):
    oauth_secret = "iframe_oauth_secret_123"
    monkeypatch.delenv("SHOPIFY_API_SECRET", raising=False)
    monkeypatch.setattr(fixer_oauth, "SHOPIFY_API_KEY", "test_api_key")
    monkeypatch.setattr(fixer_oauth, "SHOPIFY_API_SECRET", "")
    monkeypatch.setattr(fixer_utils, "SHOPIFY_API_SECRET", oauth_secret)
    shop = "iframe-cookie-blocked.myshopify.com"

    install_resp = client.get(f"/api/fixer/install?shop={shop}")
    assert install_resp.status_code == 302
    parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(install_resp.headers["Location"]).query)
    valid_state = parsed_qs["state"][0]

    client.delete_cookie("fixer_oauth_state")

    params = {"shop": shop, "code": "test_code", "state": valid_state}
    sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig = hmac.new(oauth_secret.encode("utf-8"), sorted_params.encode("utf-8"), hashlib.sha256).hexdigest()
    qs = urllib.parse.urlencode({**params, "hmac": sig})

    cb_resp = client.get(f"/api/fixer/auth/callback?{qs}")
    assert cb_resp.status_code == 302
    assert "/api/fixer/dashboard" in cb_resp.headers["Location"]

    replay_resp = client.get(f"/api/fixer/auth/callback?{qs}")
    assert replay_resp.status_code == 403
