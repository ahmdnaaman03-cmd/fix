import sqlite3
import pytest
import app as fixer_app
import oauth as fixer_oauth
import billing as fixer_billing

@pytest.fixture
def client(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_fixer.db")
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

def _insert_store(db_path, fixer_id, store_id, token="shpat_valid_test_token_12345", plan="free", sub_status="none", orders_count=0):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO stores (fixer_id, store_id, access_token, plan, subscription_status, orders_count) VALUES (?, ?, ?, ?, ?, ?)",
            (fixer_id, store_id, token, plan, sub_status, orders_count)
        )
        conn.commit()

def test_arabic_and_local_phone_e164(client):
    resp = client.post("/api/fixer/webhook/order", json={
        "store_id": "arab-shop.myshopify.com",
        "id": "1001",
        "shipping_address": {
            "phone": "٠١٠١٢٣٤٥٦٧٨",
            "country_code": "EG"
        }
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["clean_phone"] == "+201012345678"
    assert data["validation_status"] == "valid"

def test_ambiguous_phone_flagged_not_rewritten(client):
    resp = client.post("/api/fixer/webhook/order", json={
        "store_id": "arab-shop.myshopify.com",
        "id": "1005",
        "shipping_address": {
            "phone": "0501234567",
            "country_code": "EG"
        }
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["clean_phone"] is None
    assert data["validation_status"] == "invalid_format"

def test_merchant_isolation_security(client):
    resp = client.post("/api/fixer/webhook/order", json={
        "store_id": "secure-shop.myshopify.com",
        "id": "1002",
        "phone": "+201112223334"
    })
    fixer_id = resp.get_json()["fixer_id"]

    unauth_resp = client.get("/api/fixer/status/secure-shop.myshopify.com")
    assert unauth_resp.status_code == 403

    auth_resp = client.get(
        "/api/fixer/status/secure-shop.myshopify.com",
        headers={"X-Fixer-Id": fixer_id}
    )
    assert auth_resp.status_code == 200
    assert auth_resp.get_json()["orders_used"] == 1

    dash_resp = client.get(f"/api/fixer/dashboard?shop=secure-shop.myshopify.com&fixer_id={fixer_id}")
    assert dash_resp.status_code == 200

def test_free_tier_limit_and_gdpr(client):
    store = "limit-shop.myshopify.com"
    for i in range(50):
        r = client.post("/api/fixer/webhook/order", json={
            "store_id": store,
            "id": str(i + 1),
            "phone": "01099988877"
        })
        assert r.status_code == 200

    limit_resp = client.post("/api/fixer/webhook/order", json={
        "store_id": store,
        "id": "51",
        "phone": "01099988877"
    })
    assert limit_resp.get_json()["status"] == "upgrade_required"

    redact_resp = client.post("/api/fixer/webhooks/shop_redact", json={"shop_domain": store})
    assert redact_resp.status_code == 200

def test_billing_accept_and_decline_verification(client, monkeypatch):
    shop = "billing-shop.myshopify.com"
    _insert_store(fixer_billing.DB_PATH, "fx_bill1", shop)

    # 1. Merchant declines charge -> Shopify reports no ACTIVE subscriptions
    monkeypatch.setattr(fixer_oauth, "fetch_active_subscriptions_graphql", lambda s, t: [])
    resp_decline = client.get(f"/api/fixer/billing/confirm?shop={shop}&fixer_id=fx_bill1", follow_redirects=True)
    assert resp_decline.status_code == 200
    html_decline = resp_decline.get_data(as_text=True)
    assert "Plan: FREE" in html_decline
    assert "Subscription Charge Declined" in html_decline
    assert "Upgrade Now ($4.99/mo)" in html_decline

    # 2. Merchant approves charge -> Shopify reports ACTIVE subscription
    monkeypatch.setattr(
        fixer_oauth,
        "fetch_active_subscriptions_graphql",
        lambda s, t: [{"id": "gid://shopify/AppSubscription/999", "name": "Fixer Pro Plan", "status": "ACTIVE"}]
    )
    resp_accept = client.get(f"/api/fixer/billing/confirm?shop={shop}&fixer_id=fx_bill1", follow_redirects=True)
    assert resp_accept.status_code == 200
    html_accept = resp_accept.get_data(as_text=True)
    assert "Plan: PRO" in html_accept
    assert "Subscription Active" in html_accept
    assert "Upgrade Now" not in html_accept

def test_uninstall_cancels_pro_and_preserves_orders_count(client, monkeypatch):
    shop = "reinstall-shop.myshopify.com"
    _insert_store(fixer_billing.DB_PATH, "fx_re1", shop, plan="pro", sub_status="active", orders_count=49)

    # Uninstall webhook arrives
    un_resp = client.post(
        "/api/fixer/webhooks/app_uninstalled",
        headers={"X-Shopify-Shop-Domain": shop},
        json={"myshopify_domain": shop}
    )
    assert un_resp.status_code == 200

    # Verify DB: downgraded to free, marked cancelled, tokens cleared, orders_count STILL 49
    with sqlite3.connect(fixer_billing.DB_PATH) as conn:
        row = conn.execute(
            "SELECT plan, subscription_status, access_token, orders_count FROM stores WHERE store_id = ?",
            (shop,)
        ).fetchone()
    assert row == ("free", "cancelled", None, 49)

    # Simulate reinstall with fresh token and Shopify reporting no active subscriptions
    with sqlite3.connect(fixer_billing.DB_PATH) as conn:
        conn.execute("UPDATE stores SET access_token = ? WHERE store_id = ?", ("shpat_fresh_reinstall_token", shop))
        conn.commit()
    monkeypatch.setattr(fixer_oauth, "fetch_active_subscriptions_graphql", lambda s, t: [])

    dash = client.get(f"/api/fixer/dashboard?shop={shop}&fixer_id=fx_re1")
    html = dash.get_data(as_text=True)
    assert "Plan: FREE (49 / 50 orders)" in html
    assert "Previous Subscription Cancelled" in html
    assert "Resubscribe to Pro ($4.99/mo)" in html

def test_reinstall_live_sync_catches_missed_uninstall_webhook_and_allows_resubscribe(client, monkeypatch):
    shop = "missed-hook-shop.myshopify.com"
    # DB still thinks store is PRO even though merchant uninstalled & reinstalled
    _insert_store(fixer_billing.DB_PATH, "fx_miss1", shop, plan="pro", sub_status="active", orders_count=2)

    # Live GraphQL check returns no active subscriptions
    monkeypatch.setattr(fixer_oauth, "fetch_active_subscriptions_graphql", lambda s, t: [])
    dash = client.get(f"/api/fixer/dashboard?shop={shop}&fixer_id=fx_miss1")
    html = dash.get_data(as_text=True)
    assert "Plan: FREE (2 / 50 orders)" in html
    assert "Previous Subscription Cancelled" in html
    assert "Resubscribe to Pro ($4.99/mo)" in html

    # Merchant clicks Resubscribe -> redirected to Shopify Billing confirmationUrl
    monkeypatch.setattr(fixer_billing, "create_shopify_charge", lambda s, t, u: "https://admin.shopify.com/mock-confirm")
    up_resp = client.get(f"/api/fixer/billing/upgrade?shop={shop}&fixer_id=fx_miss1")
    assert up_resp.status_code == 302
    assert up_resp.headers["Location"] == "https://admin.shopify.com/mock-confirm"

    # Merchant approves resubscription -> PRO active again
    monkeypatch.setattr(
        fixer_oauth,
        "fetch_active_subscriptions_graphql",
        lambda s, t: [{"id": "gid://shopify/AppSubscription/1001", "name": "Fixer Pro Plan", "status": "ACTIVE"}]
    )
    conf_resp = client.get(f"/api/fixer/billing/confirm?shop={shop}&fixer_id=fx_miss1", follow_redirects=True)
    assert "Plan: PRO" in conf_resp.get_data(as_text=True)
