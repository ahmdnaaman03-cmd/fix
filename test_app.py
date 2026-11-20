import hmac
import json
import time
import base64
import hashlib
import sqlite3
import urllib.parse
import pytest
import db as fixer_db
import app as fixer_app
import utils as fixer_utils
import oauth as fixer_oauth
import billing as fixer_billing
import security as fixer_security

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

def _make_jwt(payload, secret):
    def _b64u(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).rstrip(b"=").decode("utf-8")
    h = _b64u({"alg": "HS256", "typ": "JWT"})
    p = _b64u(payload)
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode("utf-8"), f"{h}.{p}".encode("utf-8"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("utf-8")
    return f"{h}.{p}.{sig}"

def _sign_oauth(params, secret):
    sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "hmac")
    sig = hmac.new(secret.encode("utf-8"), sorted_params.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**params, "hmac": sig})

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

    monkeypatch.setattr(fixer_oauth, "fetch_active_subscriptions_graphql", lambda s, t: [])
    resp_decline = client.get(f"/api/fixer/billing/confirm?shop={shop}&fixer_id=fx_bill1", follow_redirects=True)
    assert resp_decline.status_code == 200
    html_decline = resp_decline.get_data(as_text=True)
    assert "Plan: FREE" in html_decline
    assert "Subscription Charge Declined" in html_decline
    assert "Upgrade Now ($4.99/mo)" in html_decline

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

    un_resp = client.post(
        "/api/fixer/webhooks/app_uninstalled",
        headers={"X-Shopify-Shop-Domain": shop},
        json={"myshopify_domain": shop}
    )
    assert un_resp.status_code == 200

    with sqlite3.connect(fixer_billing.DB_PATH) as conn:
        row = conn.execute(
            "SELECT plan, subscription_status, access_token, orders_count FROM stores WHERE store_id = ?",
            (shop,)
        ).fetchone()
    assert row == ("free", "cancelled", None, 49)

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
    _insert_store(fixer_billing.DB_PATH, "fx_miss1", shop, plan="pro", sub_status="active", orders_count=2)

    monkeypatch.setattr(fixer_oauth, "fetch_active_subscriptions_graphql", lambda s, t: [])
    dash = client.get(f"/api/fixer/dashboard?shop={shop}&fixer_id=fx_miss1")
    html = dash.get_data(as_text=True)
    assert "Plan: FREE (2 / 50 orders)" in html
    assert "Previous Subscription Cancelled" in html
    assert "Resubscribe to Pro ($4.99/mo)" in html

    monkeypatch.setattr(fixer_billing, "create_shopify_charge", lambda s, t, u: "https://admin.shopify.com/mock-confirm")
    up_resp = client.get(f"/api/fixer/billing/upgrade?shop={shop}&fixer_id=fx_miss1")
    assert up_resp.status_code == 302
    assert up_resp.headers["Location"] == "https://admin.shopify.com/mock-confirm"

    monkeypatch.setattr(
        fixer_oauth,
        "fetch_active_subscriptions_graphql",
        lambda s, t: [{"id": "gid://shopify/AppSubscription/1001", "name": "Fixer Pro Plan", "status": "ACTIVE"}]
    )
    conf_resp = client.get(f"/api/fixer/billing/confirm?shop={shop}&fixer_id=fx_miss1", follow_redirects=True)
    assert "Plan: PRO" in conf_resp.get_data(as_text=True)

def test_security_xss_escaping_and_csp_headers(client):
    shop = "xss-shop.myshopify.com"
    resp = client.post("/api/fixer/webhook/order", json={
        "store_id": shop,
        "id": "2001",
        "shipping_address": {
            "phone": "<script>alert(1)</script>",
            "country_code": "EG"
        }
    })
    fixer_id = resp.get_json()["fixer_id"]

    dash = client.get(f"/api/fixer/dashboard?shop={shop}&fixer_id={fixer_id}")
    assert dash.status_code == 200
    html = dash.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    csp = dash.headers.get("Content-Security-Policy", "")
    assert f"frame-ancestors https://{shop} https://admin.shopify.com" in csp
    assert dash.headers.get("X-Content-Type-Options") == "nosniff"

    bad_shop = client.get("/api/fixer/dashboard?shop=<script>alert(1)</script>&fixer_id=x")
    assert bad_shop.status_code == 400

def test_security_dashboard_blocks_missing_fixer_id(client):
    shop = "lock-shop.myshopify.com"
    _insert_store(fixer_billing.DB_PATH, "fx_lock1", shop)
    unauth = client.get(f"/api/fixer/dashboard?shop={shop}")
    assert unauth.status_code == 403

def test_security_oauth_state_csrf_protection(client, monkeypatch):
    oauth_secret = "oauth_test_secret_123"
    monkeypatch.delenv("SHOPIFY_API_SECRET", raising=False)
    monkeypatch.setattr(fixer_oauth, "SHOPIFY_API_KEY", "test_api_key")
    monkeypatch.setattr(fixer_oauth, "SHOPIFY_API_SECRET", "")
    monkeypatch.setattr(fixer_utils, "SHOPIFY_API_SECRET", oauth_secret)
    shop = "oauth-shop.myshopify.com"

    bad_install = client.get("/api/fixer/install?shop=evil.com")
    assert bad_install.status_code == 400

    install_resp = client.get(f"/api/fixer/install?shop={shop}")
    assert install_resp.status_code == 302
    set_cookie = install_resp.headers.get("Set-Cookie", "")
    assert "fixer_oauth_state=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie

    parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(install_resp.headers["Location"]).query)
    valid_state = parsed_qs["state"][0]

    forged_qs = _sign_oauth({"shop": shop, "code": "test_code", "state": "forged_state"}, oauth_secret)
    forged = client.get(f"/api/fixer/auth/callback?{forged_qs}")
    assert forged.status_code == 403

    valid_qs = _sign_oauth({"shop": shop, "code": "test_code", "state": valid_state}, oauth_secret)
    valid_cb = client.get(f"/api/fixer/auth/callback?{valid_qs}")
    assert valid_cb.status_code == 302
    assert "/api/fixer/dashboard" in valid_cb.headers["Location"]

    replay_cb = client.get(f"/api/fixer/auth/callback?{valid_qs}")
    assert replay_cb.status_code == 403

def test_security_jwt_session_ping_verification(client, monkeypatch):
    secret = "test_super_secret_123"
    api_key = "test_app_key_456"
    shop = "jwt-shop.myshopify.com"
    monkeypatch.setenv("SHOPIFY_API_SECRET", secret)
    monkeypatch.setenv("SHOPIFY_API_KEY", api_key)
    monkeypatch.setattr(fixer_db, "SHOPIFY_API_SECRET", secret)
    monkeypatch.setattr(fixer_db, "SHOPIFY_API_KEY", api_key)

    fake_resp = client.post(
        f"/api/fixer/session-ping?shop={shop}",
        headers={"Authorization": "Bearer fake.jwt.token"}
    )
    assert fake_resp.status_code == 401

    now = int(time.time())
    valid_token = _make_jwt(
        {"aud": api_key, "dest": f"https://{shop}", "exp": now + 120, "nbf": now - 10},
        secret
    )
    ok_resp = client.post(
        f"/api/fixer/session-ping?shop={shop}",
        headers={"Authorization": f"Bearer {valid_token}"}
    )
    assert ok_resp.status_code == 200

    expired_token = _make_jwt(
        {"aud": api_key, "dest": f"https://{shop}", "exp": now - 60},
        secret
    )
    exp_resp = client.post(
        f"/api/fixer/session-ping?shop={shop}",
        headers={"Authorization": f"Bearer {expired_token}"}
    )
    assert exp_resp.status_code == 401

    wrong_shop_token = _make_jwt(
        {"aud": api_key, "dest": "https://other-shop.myshopify.com", "exp": now + 120},
        secret
    )
    wrong_resp = client.post(
        f"/api/fixer/session-ping?shop={shop}",
        headers={"Authorization": f"Bearer {wrong_shop_token}"}
    )
    assert wrong_resp.status_code == 401

def test_security_csrf_token_on_post_upgrade(client):
    shop = "csrf-shop.myshopify.com"
    _insert_store(fixer_billing.DB_PATH, "fx_csrf1", shop, token="short")

    missing_csrf = client.post(
        "/api/fixer/billing/upgrade",
        data={"shop": shop, "fixer_id": "fx_csrf1"}
    )
    assert missing_csrf.status_code == 403

    good_token = fixer_security.generate_csrf_token(shop, "fx_csrf1")
    ok_upgrade = client.post(
        "/api/fixer/billing/upgrade",
        data={"shop": shop, "fixer_id": "fx_csrf1", "csrf_token": good_token}
    )
    assert ok_upgrade.status_code == 200
