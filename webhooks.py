import json
import uuid
import sqlite3
import urllib.request
from flask import request, jsonify
from db import DB_PATH, FREE_TIER_LIMIT
from utils import verify_shopify_hmac, extract_order_phone, validate_and_format_phone

def update_shopify_order(shop, access_token, order_id, data, clean_phone, status):
    if not access_token or not len(access_token) > 10 or order_id == "0":
        return "logged_locally"

    existing_tags = [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]
    existing_tags = [t for t in existing_tags if not t.startswith("Fixer:")]

    order_payload = {"id": order_id}
    if status == "valid" and clean_phone:
        existing_tags.append("Fixer: E.164 Verified")
        order_payload["phone"] = clean_phone
        shipping = dict(data.get("shipping_address") or {})
        if shipping:
            shipping["phone"] = clean_phone
            order_payload["shipping_address"] = shipping
        action_taken = "phone_updated_and_tagged"
    else:
        existing_tags.append("Fixer: Invalid Phone")
        action_taken = "flagged_invalid_phone"

    order_payload["tags"] = ", ".join(existing_tags)

    req = urllib.request.Request(
        f"https://{shop}/admin/api/2026-01/orders/{order_id}.json",
        data=json.dumps({"order": order_payload}).encode("utf-8"),
        headers={
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        },
        method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201):
                return action_taken
    except Exception:
        pass
    return "logged_locally"

def register_webhooks(bp):
    @bp.route("/webhook/order", methods=["POST"])
    def inspect_order():
        if not verify_shopify_hmac(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256")):
            return jsonify({"error": "invalid_hmac"}), 401

        data = request.get_json(silent=True) or {}
        store_id = request.headers.get("X-Shopify-Shop-Domain") or data.get("store_id", "demo-store.myshopify.com")
        order_id = str(data.get("id") or data.get("order_id", "0"))
        raw_phone, dial_code = extract_order_phone(data)

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT fixer_id, access_token, plan, orders_count FROM stores WHERE store_id = ?", (store_id,))
            row = cur.fetchone()
            if not row:
                fixer_id = f"fx_{uuid.uuid4().hex[:8]}"
                access_token, plan, orders_count = None, "free", 0
                cur.execute(
                    "INSERT INTO stores (fixer_id, store_id, plan, orders_count) VALUES (?, ?, ?, ?)",
                    (fixer_id, store_id, plan, orders_count)
                )
            else:
                fixer_id, access_token, plan, orders_count = row

            if plan == "free" and orders_count >= FREE_TIER_LIMIT:
                return jsonify({
                    "fixer_id": fixer_id,
                    "store_id": store_id,
                    "status": "upgrade_required",
                    "price_usd": 4.99,
                    "message": "تم استهلاك الباقة المجانية (50 طلباً). يرجى الترقية لباقة 4.99$/شهرياً."
                }), 200

            clean_phone, status = validate_and_format_phone(raw_phone, default_country_code=dial_code)
            cur.execute("UPDATE stores SET orders_count = orders_count + 1 WHERE fixer_id = ?", (fixer_id,))
            cur.execute(
                "INSERT INTO order_logs (fixer_id, store_id, order_id, raw_phone, clean_phone, status) VALUES (?, ?, ?, ?, ?, ?)",
                (fixer_id, store_id, order_id, raw_phone, clean_phone, status)
            )
            conn.commit()

        shopify_action = update_shopify_order(store_id, access_token, order_id, data, clean_phone, status)

        return jsonify({
            "fixer_id": fixer_id,
            "store_id": store_id,
            "order_id": order_id,
            "raw_phone": raw_phone,
            "clean_phone": clean_phone,
            "validation_status": status,
            "shopify_action": shopify_action,
            "orders_used": orders_count + 1,
            "free_limit": FREE_TIER_LIMIT
        }), 200

    @bp.route("/webhooks/customers_data_request", methods=["POST"])
    @bp.route("/webhooks/customers_redact", methods=["POST"])
    def gdpr_customer_hooks():
        if not verify_shopify_hmac(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256")):
            return jsonify({"error": "invalid_hmac"}), 401
        return jsonify({"status": "acknowledged"}), 200

    @bp.route("/webhooks/shop_redact", methods=["POST"])
    def gdpr_shop_redact():
        if not verify_shopify_hmac(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256")):
            return jsonify({"error": "invalid_hmac"}), 401
        data = request.get_json(silent=True) or {}
        store_id = data.get("shop_domain")
        if store_id:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("DELETE FROM order_logs WHERE store_id = ?", (store_id,))
                conn.execute("DELETE FROM stores WHERE store_id = ?", (store_id,))
                conn.commit()
        return jsonify({"status": "shop_redacted"}), 200
