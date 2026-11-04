import uuid
import sqlite3
import urllib.parse
from flask import request, redirect, jsonify
from db import DB_PATH, SHOPIFY_API_KEY, APP_URL
from utils import verify_oauth_hmac

def register_oauth(bp):
    @bp.route("/install", methods=["GET"])
    @bp.route("/auth", methods=["GET"])
    def install():
        shop = request.args.get("shop")
        if not shop:
            return jsonify({"error": "missing_shop_parameter"}), 400
        if not SHOPIFY_API_KEY:
            return jsonify({
                "status": "oauth_ready",
                "shop": shop,
                "message": "يرجى إضافة SHOPIFY_API_KEY في متغيرات البيئة لتفعيل التوجيه التلقائي."
            }), 200
        scopes = "read_orders,write_orders,read_customers"
        redirect_uri = f"{APP_URL}/api/fixer/auth/callback"
        params = urllib.parse.urlencode({
            "client_id": SHOPIFY_API_KEY,
            "scope": scopes,
            "redirect_uri": redirect_uri,
            "state": uuid.uuid4().hex[:12]
        })
        return redirect(f"https://{shop}/admin/oauth/authorize?{params}")

    @bp.route("/callback", methods=["GET"])
    @bp.route("/auth/callback", methods=["GET"])
    def callback():
        params = request.args.to_dict()
        shop = params.get("shop")
        code = params.get("code")
        if not shop or not code:
            return jsonify({"error": "missing_oauth_parameters"}), 400
        if not verify_oauth_hmac(params):
            return jsonify({"error": "invalid_oauth_hmac"}), 401

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT fixer_id FROM stores WHERE store_id = ?", (shop,))
            row = cur.fetchone()
            if not row:
                fixer_id = f"fx_{uuid.uuid4().hex[:8]}"
                cur.execute(
                    "INSERT INTO stores (fixer_id, store_id, access_token, plan, orders_count) VALUES (?, ?, ?, 'free', 0)",
                    (fixer_id, shop, code)
                )
            else:
                fixer_id = row[0]
                cur.execute("UPDATE stores SET access_token = ? WHERE store_id = ?", (code, shop))
            conn.commit()

        return jsonify({
            "status": "installed",
            "fixer_id": fixer_id,
            "store_id": shop
        }), 200
