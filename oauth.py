import sys
import time
import hmac
import json
import uuid
import sqlite3
import urllib.parse
import urllib.request
import urllib.error
from flask import request, redirect, jsonify, make_response, has_app_context, current_app
from db import DB_PATH, SHOPIFY_API_KEY, SHOPIFY_API_SECRET, APP_URL
from utils import verify_oauth_hmac
from security import is_valid_shop, generate_oauth_state

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

def fetch_active_subscriptions_graphql(shop, access_token):
    graphql_payload = {
        "query": """
        query {
          currentAppInstallation {
            activeSubscriptions {
              id
              name
              status
              test
            }
          }
        }
        """
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
        res_data = json.loads(resp.read().decode("utf-8"))
        installation = (res_data.get("data") or {}).get("currentAppInstallation") or {}
        return installation.get("activeSubscriptions") or []

def sync_store_subscription(shop, access_token=None, from_billing_confirm=False):
    if not access_token:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT access_token FROM stores WHERE store_id = ?", (shop,)).fetchone()
            access_token = row[0] if row else None

    if not access_token or len(access_token) <= 10:
        return None, None

    subs = None
    try:
        subs = fetch_active_subscriptions_graphql(shop, access_token)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            new_token = refresh_store_token(shop)
            if new_token:
                try:
                    subs = fetch_active_subscriptions_graphql(shop, new_token)
                except Exception:
                    return None, None
        else:
            return None, None
    except Exception:
        return None, None

    active_sub = next((s for s in (subs or []) if (s.get("status") or "").upper() == "ACTIVE"), None)

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT plan, subscription_status FROM stores WHERE store_id = ?", (shop,))
        row = cur.fetchone()
        if not row:
            return None, None
        current_plan, current_sub_status = row[0] or "free", row[1] or "none"

        if active_sub:
            new_plan = "pro"
            new_status = "active"
            cur.execute(
                "UPDATE stores SET plan = ?, subscription_status = ?, subscription_id = ? WHERE store_id = ?",
                (new_plan, new_status, active_sub.get("id"), shop)
            )
        else:
            new_plan = "free"
            if from_billing_confirm or current_sub_status == "pending":
                new_status = "declined"
            elif current_plan == "pro" or current_sub_status in ("active", "cancelled"):
                new_status = "cancelled"
            else:
                new_status = current_sub_status
            cur.execute(
                "UPDATE stores SET plan = ?, subscription_status = ?, subscription_id = NULL WHERE store_id = ?",
                (new_plan, new_status, shop)
            )
        conn.commit()
        return new_plan, new_status

def register_webhook_graphql(shop, access_token, topic, callback_path):
    target_url = f"{APP_URL}{callback_path}"
    mutation = """
    mutation webhookSubscriptionCreate($topic: WebhookSubscriptionTopic!,$webhookSubscription: WebhookSubscriptionInput!) {
      webhookSubscriptionCreate(topic: $topic, webhookSubscription:$webhookSubscription) {
        webhookSubscription {
          id
        }
        userErrors {
          field
          message
        }
      }
    }
    """

    def _send(token, url_field):
        graphql_payload = {
            "query": mutation,
            "variables": {
                "topic": topic,
                "webhookSubscription": {
                    url_field: target_url,
                    "format": "JSON"
                }
            }
        }
        req = urllib.request.Request(
            f"https://{shop}/admin/api/2026-01/graphql.json",
            data=json.dumps(graphql_payload).encode("utf-8"),
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    for url_field in ("uri", "callbackUrl"):
        try:
            res_data = _send(access_token, url_field)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                new_token = refresh_store_token(shop)
                if new_token:
                    access_token = new_token
                    try:
                        res_data = _send(access_token, url_field)
                    except Exception as inner_e:
                        print(f"[Fixer] Webhook {topic} failed after refresh: {inner_e}", file=sys.stderr)
                        return False
                else:
                    return False
            else:
                print(f"[Fixer] Webhook {topic} HTTP error: {e.code}", file=sys.stderr)
                return False
        except Exception as e:
            print(f"[Fixer] Webhook {topic} error: {e}", file=sys.stderr)
            return False

        gql_errors = res_data.get("errors") or []
        if gql_errors:
            err_text = json.dumps(gql_errors)
            if url_field == "uri" and "uri" in err_text:
                continue
            print(f"[Fixer] Webhook {topic} GraphQL errors: {err_text}", file=sys.stderr)
            return False

        sub_res = (res_data.get("data") or {}).get("webhookSubscriptionCreate") or {}
        user_errors = sub_res.get("userErrors") or []
        if sub_res.get("webhookSubscription"):
            return True
        if user_errors:
            msg = json.dumps(user_errors)
            if "already been taken" in msg.lower():
                return True
            print(f"[Fixer] Webhook {topic} userErrors: {msg}", file=sys.stderr)
        return False
    return False

def register_oauth(bp):
    @bp.route("/install", methods=["GET"])
    @bp.route("/auth", methods=["GET"])
    def install():
        shop = request.args.get("shop")
        next_action = request.args.get("next", "")
        if not shop:
            return jsonify({"error": "missing_shop_parameter"}), 400
        if not is_valid_shop(shop):
            return jsonify({"error": "invalid_shop_domain"}), 400
        if not SHOPIFY_API_KEY:
            return jsonify({
                "status": "oauth_ready",
                "shop": shop,
                "message": "يرجى إضافة SHOPIFY_API_KEY في متغيرات البيئة لتفعيل التوجيه التلقائي."
            }), 200
        scopes = "read_orders,write_orders,read_customers"
        redirect_uri = f"{APP_URL}/api/fixer/auth/callback"
        state_prefix = "upgrade_" if next_action == "upgrade" else ""
        state = generate_oauth_state(state_prefix)

        with sqlite3.connect(DB_PATH) as conn:
            now = time.time()
            conn.execute("DELETE FROM oauth_states WHERE created_at < ?", (now - 600,))
            conn.execute(
                "INSERT OR REPLACE INTO oauth_states (state, shop, created_at) VALUES (?, ?, ?)",
                (state, shop, now)
            )
            conn.commit()

        params = urllib.parse.urlencode({
            "client_id": SHOPIFY_API_KEY,
            "scope": scopes,
            "redirect_uri": redirect_uri,
            "state": state
        })
        resp = make_response(redirect(f"https://{shop}/admin/oauth/authorize?{params}"))
        is_testing = has_app_context() and current_app.config.get("TESTING")
        resp.set_cookie(
            "fixer_oauth_state",
            state,
            httponly=True,
            secure=not is_testing,
            samesite="Lax",
            max_age=600
        )
        return resp

    @bp.route("/callback", methods=["GET"])
    @bp.route("/auth/callback", methods=["GET"])
    def callback():
        params = request.args.to_dict()
        shop = params.get("shop")
        code = params.get("code")
        state = params.get("state", "")
        if not shop or not code:
            return jsonify({"error": "missing_oauth_parameters"}), 400
        if not is_valid_shop(shop):
            return jsonify({"error": "invalid_shop_domain"}), 400

        cookie_state = request.cookies.get("fixer_oauth_state", "")
        if not state:
            return jsonify({"error": "invalid_oauth_state"}), 403
        if cookie_state and not hmac.compare_digest(cookie_state, state):
            return jsonify({"error": "invalid_oauth_state"}), 403

        with sqlite3.connect(DB_PATH) as conn:
            now = time.time()
            conn.execute("DELETE FROM oauth_states WHERE created_at < ?", (now - 600,))
            state_row = conn.execute(
                "SELECT state FROM oauth_states WHERE state = ? AND shop = ?",
                (state, shop)
            ).fetchone()
            if not state_row:
                conn.commit()
                return jsonify({"error": "invalid_oauth_state"}), 403
            conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
            conn.commit()

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

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT fixer_id FROM stores WHERE store_id = ?", (shop,))
            row = cur.fetchone()
            if not row:
                fixer_id = f"fx_{uuid.uuid4().hex[:8]}"
                cur.execute(
                    "INSERT INTO stores (fixer_id, store_id, access_token, refresh_token, plan, subscription_status, orders_count) VALUES (?, ?, ?, ?, 'free', 'none', 0)",
                    (fixer_id, shop, access_token, refresh_token)
                )
            else:
                fixer_id = row[0]
                cur.execute(
                    "UPDATE stores SET access_token = ?, refresh_token = COALESCE(?, refresh_token) WHERE store_id = ?",
                    (access_token, refresh_token, shop)
                )
            conn.commit()

        if access_token and access_token != code:
            register_webhook_graphql(shop, access_token, "ORDERS_CREATE", "/api/fixer/webhook/order")
            register_webhook_graphql(shop, access_token, "APP_UNINSTALLED", "/api/fixer/webhooks/app_uninstalled")
            sync_store_subscription(shop, access_token)

        target = (
            f"/api/fixer/billing/upgrade?shop={shop}&fixer_id={fixer_id}"
            if state.startswith("upgrade_")
            else f"/api/fixer/dashboard?shop={shop}&fixer_id={fixer_id}"
        )
        resp = make_response(redirect(target))
        resp.delete_cookie("fixer_oauth_state")
        return resp
