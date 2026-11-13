import json
import uuid
import sqlite3
import urllib.request
import urllib.error
from flask import request, jsonify
from db import DB_PATH, FREE_TIER_LIMIT
from utils import verify_shopify_hmac, extract_order_phone, validate_and_format_phone
from oauth import refresh_store_token

def execute_order_update_graphql(shop, access_token, order_input):
    graphql_payload = {
        "query": """
        mutation orderUpdate($input: OrderInput!) {
          orderUpdate(input: $input) {
            order {
              id
              tags
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        "variables": {
            "input": order_input
        }
    }
    req = urllib.request.Request(
        f"https://{shop}/admin/api/2026-01/graphql.json",
        data=json.dumps(graphql_payload).encode("utf-8"),
        headers={
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        res_body = json.loads(resp.read().decode("utf-8"))
        user_errors = ((res_body.get("data") or {}).get("orderUpdate") or {}).get("userErrors") or []
        return resp.status in (200, 201) and not user_errors

def update_shopify_order(shop, access_token, order_id, data, clean_phone, status, is_duplicate=False):
    if not access_token or not len(access_token) > 10 or order_id == "0":
        return "logged_locally"

    existing_tags = [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]
    existing_tags = [t for t in existing_tags if not t.startswith("Fixer:")]

    order_gid = order_id if str(order_id).startswith("gid://") else f"gid://shopify/Order/{order_id}"
    order_input = {"id": order_gid}

    if status == "valid" and clean_phone:
        existing_tags.append("Fixer: E.164 Verified")
        if is_duplicate:
            existing_tags.append("Fixer: Duplicate Order")
        shipping = dict(data.get("shipping_address") or {})
        if shipping:
            addr_input = {"phone": clean_phone}
            field_map = {
                "first_name": "firstName",
                "last_name": "lastName",
                "address1": "address1",
                "address2": "address2",
                "city": "city",
                "province_code": "provinceCode",
                "country_code": "countryCode",
                "zip": "zip",
                "company": "company"
            }
            for k_snake, k_camel in field_map.items():
                if shipping.get(k_snake):
                    addr_input[k_camel] = shipping[k_snake]
            order_input["shippingAddress"] = addr_input
        action_taken = "phone_updated_and_tagged"
    else:
        existing_tags.append("Fixer: Invalid Phone")
        if is_duplicate:
            existing_tags.append("Fixer: Duplicate Order")
        action_taken = "flagged_invalid_phone"

    order_input["tags"] = existing_tags

    try:
        if execute_order_update_graphql(shop, access_token, order_input):
            return action_taken
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            new_token = refresh_store_token(shop)
            if new_token:
                try:
                    if execute_order_update_graphql(shop, new_token, order_input):
                        return action_taken
                except Exception:
                    pass
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

            if order_id != "0":
                cur.execute(
                    "SELECT raw_phone, clean_phone, status FROM order_logs WHERE store_id = ? AND order_id = ? LIMIT 1",
                    (store_id, order_id)
                )
                existing_log = cur.fetchone()
                if existing_log:
                    saved_status = "valid" if existing_log[2] == "valid_duplicate" else existing_log[2]
                    return jsonify({
                        "fixer_id": fixer_id,
                        "store_id": store_id,
                        "order_id": order_id,
                        "raw_phone": existing_log[0],
                        "clean_phone": existing_log[1],
                        "validation_status": saved_status,
                        "is_duplicate": existing_log[2] == "valid_duplicate",
                        "shopify_action": "idempotent_skip",
                        "orders_used": orders_count,
                        "free_limit": FREE_TIER_LIMIT
                    }), 200

            if plan == "free" and orders_count >= FREE_TIER_LIMIT:
                return jsonify({
                    "fixer_id": fixer_id,
                    "store_id": store_id,
                    "status": "upgrade_required",
                    "price_usd": 4.99,
                    "message": "تم استهلاك الباقة المجانية (50 طلباً). يرجى الترقية لباقة 4.99$/شهرياً."
                }), 200

            clean_phone, status = validate_and_format_phone(raw_phone, default_country_code=dial_code)

            is_duplicate = False
            if clean_phone:
                cur.execute(
                    "SELECT 1 FROM order_logs WHERE store_id = ? AND clean_phone = ? LIMIT 1",
                    (store_id, clean_phone)
                )
                if cur.fetchone():
                    is_duplicate = True

            db_status = "valid_duplicate" if (status == "valid" and is_duplicate) else status
            cur.execute("UPDATE stores SET orders_count = orders_count + 1 WHERE fixer_id = ?", (fixer_id,))
            cur.execute(
                "INSERT INTO order_logs (fixer_id, store_id, order_id, raw_phone, clean_phone, status) VALUES (?, ?, ?, ?, ?, ?)",
                (fixer_id, store_id, order_id, raw_phone, clean_phone, db_status)
            )
            conn.commit()

        shopify_action = update_shopify_order(
            store_id, access_token, order_id, data, clean_phone, status, is_duplicate=is_duplicate
        )

        return jsonify({
            "fixer_id": fixer_id,
            "store_id": store_id,
            "order_id": order_id,
            "raw_phone": raw_phone,
            "clean_phone": clean_phone,
            "validation_status": status,
            "is_duplicate": is_duplicate,
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
