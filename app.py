import os
import re
import hmac
import hashlib
import base64
import uuid
import sqlite3
from flask import Flask, Blueprint, request, jsonify

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixer.db")
FREE_TIER_LIMIT = 50
SHOPIFY_API_SECRET = os.environ.get("SHOPIFY_API_SECRET", "")

fixer_bp = Blueprint("fixer", __name__, url_prefix="/api/fixer")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stores (
                fixer_id TEXT PRIMARY KEY,
                store_id TEXT UNIQUE,
                plan TEXT DEFAULT 'free',
                orders_count INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS order_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fixer_id TEXT,
                store_id TEXT,
                order_id TEXT,
                raw_phone TEXT,
                clean_phone TEXT,
                status TEXT
            )
        """)

def verify_shopify_hmac(raw_body, hmac_header):
    if not SHOPIFY_API_SECRET or not hmac_header:
        return True
    digest = hmac.new(SHOPIFY_API_SECRET.encode("utf-8"), raw_body, hashlib.sha256).digest()
    computed_hmac = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed_hmac, hmac_header)

def validate_and_format_phone(raw_phone, default_country_code="+20"):
    if not raw_phone:
        return None, "missing_phone"
    digits = re.sub(r"[^\d+]", "", str(raw_phone))
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif digits.startswith("01") and len(digits) == 11:
        digits = default_country_code + digits[1:]
    elif not digits.startswith("+") and len(digits) >= 10:
        digits = "+" + digits
    if re.match(r"^\+\d{10,15}$", digits):
        return digits, "valid"
    return digits, "invalid_format"

@fixer_bp.route("/webhook/order", methods=["POST"])
def inspect_order():
    if not verify_shopify_hmac(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256")):
        return jsonify({"error": "invalid_hmac"}), 401

    data = request.get_json(silent=True) or {}
    store_id = request.headers.get("X-Shopify-Shop-Domain") or data.get("store_id", "demo-store.myshopify.com")
    order_id = str(data.get("id") or data.get("order_id", "0"))
    raw_phone = data.get("phone") or (data.get("customer") or {}).get("phone", "")

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

        clean_phone, status = validate_and_format_phone(raw_phone)
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

@fixer_bp.route("/status/<store_id>", methods=["GET"])
def store_status(store_id):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT fixer_id, plan, orders_count FROM stores WHERE store_id = ?", (store_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "store_not_found"}), 404
        return jsonify({
            "fixer_id": row[0],
            "store_id": store_id,
            "plan": row[1],
            "orders_used": row[2],
            "free_limit": FREE_TIER_LIMIT
        }), 200

@fixer_bp.route("/webhooks/customers_data_request", methods=["POST"])
@fixer_bp.route("/webhooks/customers_redact", methods=["POST"])
def gdpr_customer_hooks():
    return jsonify({"status": "acknowledged"}), 200

@fixer_bp.route("/webhooks/shop_redact", methods=["POST"])
def gdpr_shop_redact():
    data = request.get_json(silent=True) or {}
    store_id = data.get("shop_domain")
    if store_id:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM order_logs WHERE store_id = ?", (store_id,))
            conn.execute("DELETE FROM stores WHERE store_id = ?", (store_id,))
            conn.commit()
    return jsonify({"status": "shop_redacted"}), 200

def create_app():
    app = Flask(__name__)
    init_db()
    app.register_blueprint(fixer_bp)

    @app.route("/", methods=["GET"])
    def home():
        return jsonify({"service": "Fixer Order & Phone Validator", "status": "running"}), 200

    return app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
