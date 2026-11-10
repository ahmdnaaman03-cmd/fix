import json
import uuid
import sqlite3
import urllib.parse
import urllib.request
from flask import request, redirect, jsonify
from db import DB_PATH, SHOPIFY_API_KEY, SHOPIFY_API_SECRET, APP_URL
from utils import verify_oauth_hmac

def refresh_store_token(shop):
    if not SHOPIFY_API_KEY or not SHOPIFY_API_SECRET:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT refresh_token FROM stores WHERE store_id = ?", (shop,))
        row = cur.fetchone()
        if not row or not row[0]:
            return None
        refresh_tok = row[0]

    req = urllib.request.Request(
        f"https://{shop}/admin/oauth/access_token",
        data=json.dumps({
            "client_id": SHOPIFY_API_KEY,
            "client_secret": SHOPIFY_API_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            new_access = data.get("access_token")
            new_refresh = data.get("refresh_token", refresh_tok)
            if new_access:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        "UPDATE stores SET access_token = ?, refresh_token = ? WHERE store_id = ?",
                        (new_access, new_refresh, shop)
                    )
                    conn.commit()
                return new_access
    except Exception:
        pass
    return None

def register_oauth(bp):
    @bp.route("/install", methods=["GET"])
    @bp.route("/auth", methods=["GET"])
    def install():
        shop = request.args.get("shop")
        next_action = request.args.get("next", "")
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
        state_prefix = "upgrade_" if next_action == "upgrade" else ""
        params = urllib.parse.urlencode({
            "client_id": SHOPIFY_API_KEY,
            "scope": scopes,
            "redirect_uri": redirect_uri,
            "state": f"{state_prefix}{uuid.uuid4().hex[:10]}"
        })
        return redirect(f"https://{shop}/admin/oauth/authorize?{params}")

    @bp.route("/callback", methods=["GET"])
    @bp.route("/auth/callback", methods=["GET"])
    def callback():
        params = request.args.to_dict()
        shop = params.get("shop")
        code = params.get("code")
        state = params.get("state", "")
        if not shop or not code:
            return jsonify({"error": "missing_oauth_parameters"}), 400
        if not verify_oauth_hmac(params):
            return jsonify({"error": "invalid_oauth_hmac"}), 401

        access_token = code
        refresh_token = None
        if SHOPIFY_API_KEY and SHOPIFY_API_SECRET:
            token_req = urllib.request.Request(
                f"https://{shop}/admin/oauth/access_token",
                data=json.dumps({
                    "client_id": SHOPIFY_API_KEY,
                    "client_secret": SHOPIFY_API_SECRET,
                    "code": code,
                    "expiring": 1
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            try:
                with urllib.request.urlopen(token_req, timeout=10) as resp:
                    token_data = json.loads(resp.read().decode("utf-8"))
                    access_token = token_data.get("access_token", code)
                    refresh_token = token_data.get("refresh_token")
            except Exception:
                pass

        if access_token and access_token != code:
            webhook_req = urllib.request.Request(
                f"https://{shop}/admin/api/2026-01/webhooks.json",
                data=json.dumps({
                    "webhook": {
                        "topic": "orders/create",
                        "address": f"{APP_URL}/api/fixer/webhook/order",
                        "format": "json"
                    }
                }).encode("utf-8"),
                headers={
                    "X-Shopify-Access-Token": access_token,
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            try:
                urllib.request.urlopen(webhook_req, timeout=10)
            except Exception:
                pass

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT fixer_id FROM stores WHERE store_id = ?", (shop,))
            row = cur.fetchone()
            if not row:
                fixer_id = f"fx_{uuid.uuid4().hex[:8]}"
                cur.execute(
                    "INSERT INTO stores (fixer_id, store_id, access_token, refresh_token, plan, orders_count) VALUES (?, ?, ?, ?, 'free', 0)",
                    (fixer_id, shop, access_token, refresh_token)
                )
            else:
                fixer_id = row[0]
                cur.execute(
                    "UPDATE stores SET access_token = ?, refresh_token = COALESCE(?, refresh_token) WHERE store_id = ?",
                    (access_token, refresh_token, shop)
                )
            conn.commit()

        if state.startswith("upgrade_"):
            return redirect(f"/api/fixer/billing/upgrade?shop={shop}&fixer_id={fixer_id}")
        return redirect(f"/api/fixer/dashboard?shop={shop}&fixer_id={fixer_id}")
