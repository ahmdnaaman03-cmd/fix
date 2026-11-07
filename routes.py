import hmac
import sqlite3
from flask import request, jsonify, render_template_string
from db import DB_PATH, FREE_TIER_LIMIT

PRIVACY_HTML = """
<!DOCTYPE html>
<html>
<head><title>Fixer - Privacy Policy</title><meta charset="utf-8"></head>
<body style="font-family: sans-serif; max-width: 800px; margin: 40px auto; line-height: 1.6; padding: 0 20px;">
  <h2>Privacy Policy for Fixer Order & Phone Validator</h2>
  <p>Last updated: November 2026</p>
  <p>Fixer processes customer phone numbers provided during Shopify checkout strictly to format them into the standard E.164 international format to prevent delivery failures.</p>
  <h3>Data We Process</h3>
  <p>We temporarily access order details (shipping address, customer phone number) via Shopify Webhooks solely to perform phone number validation and normalization.</p>
  <h3>Data Retention</h3>
  <p>We do not sell, rent, or share personal data. Logs containing sanitized order IDs and formatting statuses are maintained securely and deleted in compliance with Shopify GDPR requirements.</p>
  <h3>Contact</h3>
  <p>For any privacy inquiries, please contact: ahmedsaied7@icloud.com</p>
</body>
</html>
"""

def register_routes(bp):
    @bp.route("/", methods=["GET"])
    def index():
        return jsonify({
            "service": "Fixer Order & Phone Validator",
            "status": "active",
            "free_tier_limit": FREE_TIER_LIMIT
        }), 200

    @bp.route("/privacy", methods=["GET"])
    def privacy():
        return render_template_string(PRIVACY_HTML), 200

    @bp.route("/status/<store_id>", methods=["GET"])
    def store_status(store_id):
        provided_fixer_id = (
            request.headers.get("X-Fixer-Id")
            or request.args.get("fixer_id")
            or ""
        )

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT fixer_id, plan, orders_count FROM stores WHERE store_id = ?", (store_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "store_not_found"}), 404

            actual_fixer_id = row[0]
            if not provided_fixer_id or not hmac.compare_digest(actual_fixer_id, provided_fixer_id):
                return jsonify({"error": "unauthorized_store_access"}), 403

            return jsonify({
                "fixer_id": actual_fixer_id,
                "store_id": store_id,
                "plan": row[1],
                "orders_used": row[2],
                "free_limit": FREE_TIER_LIMIT
            }), 200
