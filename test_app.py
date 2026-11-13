import os
import json
import sqlite3
import pytest
import app as fixer_app

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
    assert data["is_duplicate"] is False

def test_idempotency_and_duplicate_order_detection(client):
    # First order
    r1 = client.post("/api/fixer/webhook/order", json={
        "store_id": "dup-shop.myshopify.com",
        "id": "2001",
        "shipping_address": {"phone": "01012345678", "country_code": "EG"}
    })
    assert r1.get_json()["orders_used"] == 1
    assert r1.get_json()["is_duplicate"] is False

    # Same order_id sent again by Shopify (Idempotency) -> should NOT increment quota
    r_retry = client.post("/api/fixer/webhook/order", json={
        "store_id": "dup-shop.myshopify.com",
        "id": "2001",
        "shipping_address": {"phone": "01012345678", "country_code": "EG"}
    })
    assert r_retry.get_json()["shopify_action"] == "idempotent_skip"
    assert r_retry.get_json()["orders_used"] == 1

    # Different order_id with same phone (Duplicate Order) -> flags is_duplicate True
    r2 = client.post("/api/fixer/webhook/order", json={
        "store_id": "dup-shop.myshopify.com",
        "id": "2002",
        "shipping_address": {"phone": "+201012345678", "country_code": "EG"}
    })
    assert r2.get_json()["orders_used"] == 2
    assert r2.get_json()["is_duplicate"] is True

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

def test_graphql_order_update_and_billing_mock(client, monkeypatch):
    captured_requests = []

    class DummyResp:
        status = 200
        def read(self):
            return json.dumps({
                "data": {
                    "orderUpdate": {"order": {"id": "gid://shopify/Order/999"}, "userErrors": []},
                    "appSubscriptionCreate": {"confirmationUrl": "https://shopify.com/confirm_pro", "userErrors": []}
                }
            }).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def fake_urlopen(req, timeout=10):
        captured_requests.append(json.loads(req.data.decode("utf-8")))
        return DummyResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with sqlite3.connect(fixer_app.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO stores (fixer_id, store_id, access_token, plan, orders_count) VALUES (?, ?, ?, 'free', 0)",
            ("fx_test123", "graphql-shop.myshopify.com", "shpat_valid_token_12345")
        )
        conn.commit()

    resp = client.post("/api/fixer/webhook/order", json={
        "store_id": "graphql-shop.myshopify.com",
        "id": "999",
        "shipping_address": {"phone": "٠١٠١٢٣٤٥٦٧٨", "country_code": "EG"}
    })
    assert resp.get_json()["shopify_action"] == "phone_updated_and_tagged"
    assert "orderUpdate" in captured_requests[0]["query"]
    assert captured_requests[0]["variables"]["input"]["shippingAddress"]["phone"] == "+201012345678"

    bill_resp = client.get("/api/fixer/billing/upgrade?shop=graphql-shop.myshopify.com&fixer_id=fx_test123")
    assert bill_resp.status_code == 302
    assert "appSubscriptionCreate" in captured_requests[1]["query"]

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
    for i in range(1, 51):
        r = client.post("/api/fixer/webhook/order", json={
            "store_id": store,
            "id": str(i),
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
