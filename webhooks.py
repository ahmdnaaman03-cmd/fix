import uuid
import sqlite3
from flask import request, jsonify
from db import DB_PATH, FREE_TIER_LIMIT
from utils import verify_shopify_hmac, extract_order_phone, validate_and_format_phone

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
            cur.execute("SELECT fixer_id, plan, orders_count FROM stores WHERE store_id = ?", (store_id,))
            row = cur.fetchone()
            if not row:
                fixer_id = f"fx_{uuid.uuid4().hex[:8]}"
                plan, orders_count = "free", 0
                cur.execute(
                    "INSERT INTO stores (fixer_id, store_id, plan, orders_count) VALUES (?, ?, ?, ?)",
                    (fixer_id, store_id, plan, orders_count)
                )
            else:
                fixer_id, plan, orders_count = row

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

        return jsonify({
            "fixer_id": fixer_id,
            "store_id": store_id,
            "order_id": order_id,
            "raw_phone": raw_phone,
            "clean_phone": clean_phone,
            "validation_status": status,
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
