import hmac
import sqlite3
from flask import request, jsonify
from db import DB_PATH, FREE_TIER_LIMIT

def register_routes(bp):
    @bp.route("/", methods=["GET"])
    def index():
        return jsonify({
            "service": "Fixer Order & Phone Validator",
            "status": "active",
            "free_tier_limit": FREE_TIER_LIMIT
        }), 200

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
