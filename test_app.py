import os
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
