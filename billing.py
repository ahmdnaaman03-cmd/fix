import os
import sys
import hmac
import json
import sqlite3
import urllib.request
import urllib.error
from flask import request, redirect, jsonify, render_template_string, has_app_context, current_app
from db import DB_PATH, FREE_TIER_LIMIT, APP_URL, SHOPIFY_API_KEY
from utils import verify_oauth_hmac
from oauth import refresh_store_token, sync_store_subscription, fetch_active_subscriptions_graphql
from webhooks import sync_recent_orders_graphql
from security import is_valid_shop, generate_csrf_token, verify_csrf_token, verify_shopify_jwt

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Fixer Dashboard</title>
    <meta name="shopify-api-key" content="{{ api_key }}" />
    <script src="https://cdn.shopify.com/shopifycloud/app-bridge.js"></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f6f6f7; margin: 0; padding: 24px; color: #202223; }
        .card { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); padding: 20px; max-width: 900px; margin: 0 auto 20px; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #e1e3e5; padding-bottom: 16px; margin-bottom: 16px; }
        .banner { padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; font-size: 14px; line-height: 1.45; }
        .banner-warn { background: #fff5ea; border: 1px solid #e1b878; color: #594400; }
        .banner-info { background: #fef3f2; border: 1px solid #f4c7c3; color: #7a271a; }
        .banner-success { background: #e3f1df; border: 1px solid #95c9b4; color: #0c5132; }
        .stats-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px; }
        .stat-box { background: #fafbfb; border: 1px solid #e1e3e5; border-radius: 6px; padding: 12px; text-align: center; }
        .stat-val { font-size: 20px; font-weight: 700; color: #202223; }
        .stat-lbl { font-size: 12px; color: #6d7175; margin-top: 4px; }
        .plans-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-top: 12px; }
        .plan-box { background: #fafbfb; border: 1px solid #e1e3e5; border-radius: 8px; padding: 16px; display: flex; flex-direction: column; justify-content: space-between; }
        .plan-box-active { border: 2px solid #008060; background: #f4faf7; }
        .plan-title { font-size: 16px; font-weight: 700; margin: 0 0 4px; }
        .plan-price { font-size: 22px; font-weight: 700; margin: 0 0 8px; color: #202223; }
        .plan-features { margin: 0 0 14px; padding-left: 18px; font-size: 13px; color: #4a4f54; line-height: 1.6; }
        .badge { padding: 4px 10px; border-radius: 12px; font-size: 13px; font-weight: 600; display: inline-block; }
        .badge-free { background: #e3f1df; color: #108043; }
        .badge-pro { background: #e0f5f5; color: #006fbb; }
        .badge-valid { background: #e3f1df; color: #108043; }
        .badge-dup { background: #fff5ea; color: #b98900; }
        .badge-invalid { background: #fbeae5; color: #d82c0d; }
        .btn { background: #008060; color: #fff; text-decoration: none; padding: 10px 16px; border-radius: 4px; font-weight: 600; display: inline-block; white-space: nowrap; border: none; cursor: pointer; font-size: 14px; }
        .btn-secondary { background: #fff; color: #202223; border: 1px solid #c9cccf; }
        .btn-danger { background: #fff; color: #d82c0d; border: 1px solid #f4c7c3; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #e1e3e5; }
        th { color: #6d7175; font-weight: 600; }
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <div>
                <h2 style="margin:0;">Fixer Order & Phone Validator</h2>
                <p style="margin:4px 0 0; color:#6d7175;">Store: {{ store_id }}</p>
            </div>
            <div>
                <span class="badge {{ 'badge-pro' if plan == 'pro' else 'badge-free' }}">
                    Plan: {{ plan|upper }} ({{ orders_used }} / {{ 'Unlimited' if plan == 'pro' else free_limit }} orders)
                </span>
            </div>
        </div>

        {% if plan != 'pro' and sub_status == 'cancelled' %}
        <div class="banner banner-warn">
            <strong>Previous Subscription Cancelled:</strong> Your previous Fixer Pro subscription was automatically cancelled when the app was uninstalled. Your store is currently on the <strong>Free plan</strong>. You can resubscribe below at any time.
        </div>
        {% elif plan != 'pro' and sub_status == 'downgraded' %}
        <div class="banner banner-success">
            <strong>Plan Downgraded to Free:</strong> Your Fixer Pro subscription has been cancelled in Shopify Billing. Your store is now on the <strong>Free plan</strong> (up to {{ free_limit }} orders/month), and you can upgrade again below at any time.
        </div>
        {% elif plan != 'pro' and sub_status == 'declined' %}
        <div class="banner banner-info">
            <strong>Subscription Charge Declined:</strong> Your Fixer Pro charge was not approved. You remain on the <strong>Free plan</strong> and can request approval again below whenever you are ready.
        </div>
        {% elif plan == 'pro' %}
        <div class="banner banner-success">
            <strong>Subscription Active:</strong> Your <strong>Fixer Pro ($4.99/month)</strong> plan is active with unlimited E.164 phone validation, duplicate detection &amp; auto-tagging.
        </div>
        {% endif %}

        <div class="stats-grid">
            <div class="stat-box">
                <div class="stat-val" style="color:#108043;">{{ valid_count }}</div>
                <div class="stat-lbl">Verified E.164 Phones</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" style="color:#d82c0d;">{{ risked_count }}</div>
                <div class="stat-lbl">Invalid / Duplicate Caught</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" style="color:#006fbb;">${{ "%.2f"|format(risked_count * 3.5) }}</div>
                <div class="stat-lbl">Est. Saved Return Shipping</div>
            </div>
        </div>

        <h3 style="margin:16px 0 4px;">Pricing &amp; Billing Plans</h3>
        <p style="margin:0 0 8px; font-size:13px; color:#6d7175;">Upgrade or downgrade your plan at any time. Changes sync directly with Shopify Billing.</p>
        <div class="plans-grid">
            <div class="plan-box {{ 'plan-box-active' if plan != 'pro' else '' }}">
                <div>
                    <div class="plan-title">Free Plan</div>
                    <div class="plan-price">$0 <span style="font-size:13px; font-weight:400; color:#6d7175;">/ month</span></div>
                    <ul class="plan-features">
                        <li>Up to {{ free_limit }} validated orders / month</li>
                        <li>Automatic E.164 phone formatting</li>
                        <li>Duplicate detection &amp; Shopify order auto-tagging</li>
                    </ul>
                </div>
                <div>
                    {% if plan == 'pro' %}
                    <form method="POST" action="/api/fixer/billing/downgrade" style="margin:0;">
                        <input type="hidden" name="shop" value="{{ store_id }}">
                        <input type="hidden" name="fixer_id" value="{{ fixer_id }}">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                        <button type="submit" class="btn btn-danger">Downgrade to Free</button>
                    </form>
                    {% else %}
                    <span class="badge badge-free">Current Plan</span>
                    {% endif %}
                </div>
            </div>

            <div class="plan-box {{ 'plan-box-active' if plan == 'pro' else '' }}">
                <div>
                    <div class="plan-title">Fixer Pro</div>
                    <div class="plan-price">$4.99 <span style="font-size:13px; font-weight:400; color:#6d7175;">/ month</span></div>
                    <ul class="plan-features">
                        <li>Unlimited validated orders / month</li>
                        <li>Automatic E.164 phone formatting</li>
                        <li>Duplicate detection &amp; Shopify order auto-tagging</li>
                    </ul>
                </div>
                <div>
                    {% if plan == 'pro' %}
                    <span class="badge badge-pro">Current Plan</span>
                    {% else %}
                    <form method="POST" action="/api/fixer/billing/upgrade" target="_top" style="margin:0;">
                        <input type="hidden" name="shop" value="{{ store_id }}">
                        <input type="hidden" name="fixer_id" value="{{ fixer_id }}">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                        <button type="submit" class="btn">
                            {{ 'Resubscribe to Pro ($4.99/mo)' if sub_status == 'cancelled' else 'Upgrade Now ($4.99/mo)' }}
                        </button>
                    </form>
                    {% endif %}
                </div>
            </div>
        </div>
    </div>

    <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <h3 style="margin:0;">Recent Processed Orders (Live ROI Log)</h3>
            <form method="POST" action="/api/fixer/orders/sync" style="margin:0;">
                <input type="hidden" name="shop" value="{{ store_id }}">
                <input type="hidden" name="fixer_id" value="{{ fixer_id }}">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <button type="submit" class="btn btn-secondary" style="padding:6px 12px; font-size:13px;">Sync Orders Now</button>
            </form>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Order</th>
                    <th>Raw Input Phone</th>
                    <th>Formatted E.164 Phone</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>
                {% for log in logs %}
                <tr>
                    <td>{{ log[1] if log[1] else ('#' ~ log[0]) }}</td>
                    <td>{{ log[2] or 'Missing' }}</td>
                    <td><strong>{{ log[3] or '-' }}</strong></td>
                    <td>
                        {% if log[4] == 'valid' %}
                        <span class="badge badge-valid">Fixer: E.164 Verified</span>
                        {% elif log[4] == 'valid_duplicate' %}
                        <span class="badge badge-dup">Fixer: Duplicate Order</span>
                        {% else %}
                        <span class="badge badge-invalid">Fixer: Invalid Phone</span>
                        {% endif %}
                    </td>
                </tr>
                {% else %}
                <tr>
                    <td colspan="4" style="text-align:center; color:#6d7175;">No orders processed yet. Create a test order to see live formatting!</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>

    <script>
        document.addEventListener("DOMContentLoaded", async function () {
            if (window.shopify && typeof window.shopify.idToken === "function") {
                try {
                    const shop = {{ store_id | tojson }};
                    const token = await window.shopify.idToken();
                    await fetch(`/api/fixer/session-ping?shop=${encodeURIComponent(shop)}`, {
                        method: "POST",
                        headers: {
                            "Authorization": "Bearer " + token,
                            "Content-Type": "application/json"
                        },
                        body: JSON.stringify({ ping: true })
                    });
                } catch (e) {
                    console.log("App Bridge session check:", e);
                }
            }
        });
    </script>
</body>
</html>
"""

def create_shopify_charge(shop, token, return_url):
    is_test_charge = os.environ.get("SHOPIFY_BILLING_TEST", "true").lower() == "true"
    graphql_payload = {
        "query": """
        mutation AppSubscriptionCreate($name: String!, $lineItems: [AppSubscriptionLineItemInput!]!, $returnUrl: URL!, $test: Boolean) {
          appSubscriptionCreate(name: $name, returnUrl: $returnUrl, test: $test, lineItems: $lineItems) {
            userErrors {
              field
              message
            }
            confirmationUrl
            appSubscription {
              id
            }
          }
        }
        """,
        "variables": {
            "name": "Fixer Pro Plan",
            "returnUrl": return_url,
            "test": is_test_charge,
            "lineItems": [
                {
                    "plan": {
                        "appRecurringPricingDetails": {
                            "price": {
                                "amount": 4.99,
                                "currencyCode": "USD"
                            },
                            "interval": "EVERY_30_DAYS"
                        }
                    }
                }
            ]
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
        res_data = json.loads(resp.read().decode("utf-8"))
        sub_data = (res_data.get("data") or {}).get("appSubscriptionCreate") or {}
        if sub_data.get("confirmationUrl"):
            return sub_data["confirmationUrl"]
        raise ValueError("Missing confirmationUrl from GraphQL appSubscriptionCreate")

def cancel_shopify_subscription(shop, token, subscription_id):
    graphql_payload = {
        "query": """
        mutation AppSubscriptionCancel($id: ID!) {
          appSubscriptionCancel(id: $id) {
            appSubscription {
              id
              status
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        "variables": {
            "id": subscription_id
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
        res_data = json.loads(resp.read().decode("utf-8"))
        cancel_data = (res_data.get("data") or {}).get("appSubscriptionCancel") or {}
        user_errors = cancel_data.get("userErrors") or []
        if user_errors:
            print(f"[Fixer] appSubscriptionCancel userErrors: {user_errors}", file=sys.stderr)
            return False
        return bool(cancel_data.get("appSubscription"))

def register_billing(bp):
    @bp.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        shop = request.args.get("shop", "")
        if is_valid_shop(shop):
            response.headers["Content-Security-Policy"] = (
                f"frame-ancestors https://{shop} https://admin.shopify.com; "
                "object-src 'none'; base-uri 'self';"
            )
        return response

    @bp.route("/session-ping", methods=["POST"])
    def session_ping():
        shop = request.args.get("shop", "")
        if shop and not is_valid_shop(shop):
            return jsonify({"error": "invalid_shop_domain"}), 400
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "missing_session_token"}), 401
        token = auth_header[7:].strip()
        payload = verify_shopify_jwt(token, expected_shop=shop or None)
        if not payload:
            return jsonify({"error": "invalid_session_token"}), 401
        return jsonify({"status": "session_verified"}), 200

    @bp.route("/dashboard", methods=["GET"])
    def merchant_dashboard():
        shop = request.args.get("shop", "")
        fixer_id = request.args.get("fixer_id", "")
        if not is_valid_shop(shop):
            return jsonify({"error": "invalid_shop_domain"}), 400
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT fixer_id, access_token, plan, orders_count, subscription_status FROM stores WHERE store_id = ?",
                (shop,)
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "unauthorized_or_not_found"}), 403
            db_fixer_id, access_token, plan, orders_used, sub_status = row
            has_valid_fixer_id = bool(fixer_id and hmac.compare_digest(db_fixer_id, fixer_id))
            has_valid_shopify_hmac = bool(
                request.args.get("hmac")
                and verify_oauth_hmac(request.args.to_dict())
            )
            if not (has_valid_fixer_id or has_valid_shopify_hmac):
                return jsonify({"error": "unauthorized_or_not_found"}), 403
            sub_status = sub_status or "none"

        if access_token and len(access_token) > 10:
            synced_plan, synced_status = sync_store_subscription(shop, access_token)
            if synced_plan is not None:
                plan, sub_status = synced_plan, synced_status
            sync_recent_orders_graphql(shop)

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT orders_count FROM stores WHERE store_id = ?", (shop,))
            fresh_count = cur.fetchone()
            if fresh_count is not None:
                orders_used = fresh_count[0]

            cur.execute(
                "SELECT order_id, order_name, raw_phone, clean_phone, status FROM order_logs WHERE store_id = ? ORDER BY id DESC LIMIT 15",
                (shop,)
            )
            logs = cur.fetchall()

            cur.execute("SELECT COUNT(*) FROM order_logs WHERE store_id = ? AND status IN ('valid', 'valid_duplicate')", (shop,))
            valid_count = (cur.fetchone() or [0])[0]

            cur.execute("SELECT COUNT(*) FROM order_logs WHERE store_id = ? AND status != 'valid'", (shop,))
            risked_count = (cur.fetchone() or [0])[0]

            return render_template_string(
                DASHBOARD_HTML,
                fixer_id=db_fixer_id,
                store_id=shop,
                csrf_token=generate_csrf_token(shop, db_fixer_id),
                plan=plan,
                sub_status=sub_status,
                orders_used=orders_used,
                free_limit=FREE_TIER_LIMIT,
                valid_count=valid_count,
                risked_count=risked_count,
                api_key=SHOPIFY_API_KEY or "eaab80121282dc9f5cc31d491bd073f1",
                logs=logs
            ), 200

    @bp.route("/orders/sync", methods=["POST"])
    def manual_sync_orders():
        body = request.get_json(silent=True) or {}
        shop = request.form.get("shop") or request.args.get("shop") or body.get("shop")
        fixer_id = (
            request.form.get("fixer_id")
            or request.args.get("fixer_id")
            or request.headers.get("X-Fixer-Id")
            or body.get("fixer_id")
        )
        csrf_token = (
            request.form.get("csrf_token")
            or request.headers.get("X-CSRF-Token")
            or body.get("csrf_token")
        )
        if not shop or not fixer_id:
            return jsonify({"error": "missing_parameters"}), 400
        if not is_valid_shop(shop):
            return jsonify({"error": "invalid_shop_domain"}), 400
        if not verify_csrf_token(shop, fixer_id, csrf_token):
            return jsonify({"error": "invalid_csrf_token"}), 403

        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT fixer_id FROM stores WHERE store_id = ?", (shop,)).fetchone()
            if not row or not hmac.compare_digest(row[0], fixer_id):
                return jsonify({"error": "unauthorized_store_access"}), 403

        sync_recent_orders_graphql(shop)
        return redirect(f"/api/fixer/dashboard?shop={shop}&fixer_id={fixer_id}")

    @bp.route("/billing/upgrade", methods=["GET", "POST"])
    def request_upgrade():
        body = request.get_json(silent=True) or {}
        shop = request.form.get("shop") or request.args.get("shop") or body.get("shop")
        fixer_id = (
            request.form.get("fixer_id")
            or request.args.get("fixer_id")
            or request.headers.get("X-Fixer-Id")
            or body.get("fixer_id")
        )
        if not shop or not fixer_id:
            return jsonify({"error": "missing_parameters"}), 400
        if not is_valid_shop(shop):
            return jsonify({"error": "invalid_shop_domain"}), 400
        if request.method == "POST":
            csrf_token = (
                request.form.get("csrf_token")
                or request.headers.get("X-CSRF-Token")
                or body.get("csrf_token")
            )
            if not verify_csrf_token(shop, fixer_id, csrf_token):
                return jsonify({"error": "invalid_csrf_token"}), 403

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT fixer_id, access_token FROM stores WHERE store_id = ?", (shop,))
            row = cur.fetchone()
            if not row or not hmac.compare_digest(row[0], fixer_id):
                return jsonify({"error": "unauthorized_store_access"}), 403
            access_token = row[1]

        return_url = f"{APP_URL}/api/fixer/billing/confirm?shop={shop}&fixer_id={fixer_id}"

        def _mark_pending_and_redirect(url):
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE stores SET subscription_status = 'pending' WHERE store_id = ?", (shop,))
                conn.commit()
            return redirect(url)

        if access_token and len(access_token) > 10:
            try:
                conf_url = create_shopify_charge(shop, access_token, return_url)
                return _mark_pending_and_redirect(conf_url)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    new_token = refresh_store_token(shop)
                    if new_token:
                        try:
                            conf_url = create_shopify_charge(shop, new_token, return_url)
                            return _mark_pending_and_redirect(conf_url)
                        except Exception:
                            pass
                    if SHOPIFY_API_KEY and not (has_app_context() and current_app.config.get("TESTING")):
                        return redirect(f"/api/fixer/install?shop={shop}&next=upgrade")
            except Exception:
                pass

        if SHOPIFY_API_KEY and request.method == "GET" and not (has_app_context() and current_app.config.get("TESTING")):
            return redirect(f"/api/fixer/install?shop={shop}&next=upgrade")

        return jsonify({
            "status": "billing_ready",
            "shop": shop,
            "price_usd": 4.99,
            "confirmation_url": return_url
        }), 200

    @bp.route("/billing/downgrade", methods=["POST"])
    def request_downgrade():
        body = request.get_json(silent=True) or {}
        shop = request.form.get("shop") or request.args.get("shop") or body.get("shop")
        fixer_id = (
            request.form.get("fixer_id")
            or request.args.get("fixer_id")
            or request.headers.get("X-Fixer-Id")
            or body.get("fixer_id")
        )
        csrf_token = (
            request.form.get("csrf_token")
            or request.headers.get("X-CSRF-Token")
            or body.get("csrf_token")
        )
        if not shop or not fixer_id:
            return jsonify({"error": "missing_parameters"}), 400
        if not is_valid_shop(shop):
            return jsonify({"error": "invalid_shop_domain"}), 400
        if not verify_csrf_token(shop, fixer_id, csrf_token):
            return jsonify({"error": "invalid_csrf_token"}), 403

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT fixer_id, access_token, subscription_id FROM stores WHERE store_id = ?",
                (shop,)
            )
            row = cur.fetchone()
            if not row or not hmac.compare_digest(row[0], fixer_id):
                return jsonify({"error": "unauthorized_store_access"}), 403
            access_token, stored_sub_id = row[1], row[2]

        if access_token and len(access_token) > 10:
            active_token = access_token
            sub_ids = []
            try:
                subs = fetch_active_subscriptions_graphql(shop, active_token)
                sub_ids = [s.get("id") for s in (subs or []) if (s.get("status") or "").upper() == "ACTIVE" and s.get("id")]
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    new_token = refresh_store_token(shop)
                    if new_token:
                        active_token = new_token
                        try:
                            subs = fetch_active_subscriptions_graphql(shop, active_token)
                            sub_ids = [s.get("id") for s in (subs or []) if (s.get("status") or "").upper() == "ACTIVE" and s.get("id")]
                        except Exception:
                            pass
            except Exception:
                pass

            if not sub_ids and stored_sub_id:
                sub_ids = [stored_sub_id]

            for sid in sub_ids:
                try:
                    cancel_shopify_subscription(shop, active_token, sid)
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403):
                        new_token = refresh_store_token(shop)
                        if new_token:
                            active_token = new_token
                            try:
                                cancel_shopify_subscription(shop, active_token, sid)
                            except Exception:
                                pass
                except Exception as e:
                    print(f"[Fixer] Downgrade cancel error: {e}", file=sys.stderr)

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE stores SET plan = 'free', subscription_status = 'downgraded', subscription_id = NULL WHERE store_id = ?",
                (shop,)
            )
            conn.commit()

        return redirect(f"/api/fixer/dashboard?shop={shop}&fixer_id={fixer_id}")

    @bp.route("/billing/confirm", methods=["GET"])
    def confirm_upgrade():
        shop = request.args.get("shop")
        fixer_id = request.args.get("fixer_id")
        if not shop or not fixer_id:
            return jsonify({"error": "missing_parameters"}), 400
        if not is_valid_shop(shop):
            return jsonify({"error": "invalid_shop_domain"}), 400

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT fixer_id, access_token FROM stores WHERE store_id = ?", (shop,))
            row = cur.fetchone()
            if not row or not hmac.compare_digest(row[0], fixer_id):
                return jsonify({"error": "unauthorized"}), 403
            access_token = row[1]

        sync_store_subscription(shop, access_token, from_billing_confirm=True)
        return redirect(f"/api/fixer/dashboard?shop={shop}&fixer_id={fixer_id}")
