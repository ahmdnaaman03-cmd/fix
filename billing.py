import json
import sqlite3
import urllib.request
from flask import request, redirect, jsonify, render_template_string
from db import DB_PATH, FREE_TIER_LIMIT, APP_URL

DASHBOARD_HTML = """
<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head><meta charset="utf-8"><title>Fixer Dashboard</title></head>
<body style="font-family:sans-serif; padding:20px; background:#f6f6f7; color:#202223;">
  <div style="max-width:600px; margin:auto; background:#fff; padding:24px; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,0.1);">
    <h2 style="margin-top:0;">لوحة تحكم Fixer (مدمجة داخل شوبيفاي)</h2>
    <p><strong>المتجر:</strong> {{ store_id }}</p>
    <p><strong>الباقة الحالية:</strong> {{ plan }}</p>
    <p><strong>الطلبات المستهلكة:</strong> {{ orders_used }} من {{ free_limit }}</p>
    {% if plan == 'free' %}
      <a href="/api/fixer/billing/upgrade?shop={{ store_id }}&fixer_id={{ fixer_id }}"
         style="display:inline-block; padding:10px 16px; background:#008060; color:#fff; text-decoration:none; border-radius:4px;">
         الترقية للباقة غير المحدودة ($4.99/شهرياً)
      </a>
    {% else %}
      <span style="color:#008060; font-weight:bold;">باقتك المدفوعة مفعلة بنجاح</span>
    {% endif %}
  </div>
</body>
</html>
"""

def register_billing(bp):
    @bp.route("/dashboard", methods=["GET"])
    def merchant_dashboard():
        shop = request.args.get("shop", "")
        fixer_id = request.args.get("fixer_id", "")
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT fixer_id, plan, orders_count FROM stores WHERE store_id = ?", (shop,))
            row = cur.fetchone()
            if not row or (fixer_id and row[0] != fixer_id):
                return jsonify({"error": "unauthorized_or_not_found"}), 403
            return render_template_string(
                DASHBOARD_HTML,
                fixer_id=row[0],
                store_id=shop,
                plan=row[1],
                orders_used=row[2],
                free_limit=FREE_TIER_LIMIT
            ), 200

    @bp.route("/billing/upgrade", methods=["GET", "POST"])
    def request_upgrade():
        shop = request.args.get("shop") or (request.get_json(silent=True) or {}).get("shop")
        fixer_id = request.args.get("fixer_id") or request.headers.get("X-Fixer-Id")
        if not shop or not fixer_id:
            return jsonify({"error": "missing_parameters"}), 400

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT fixer_id, access_token FROM stores WHERE store_id = ?", (shop,))
            row = cur.fetchone()
            if not row or row[0] != fixer_id:
                return jsonify({"error": "unauthorized_store_access"}), 403
            access_token = row[1]

        return_url = f"{APP_URL}/api/fixer/billing/confirm?shop={shop}&fixer_id={fixer_id}"
        payload = {
            "recurring_application_charge": {
                "name": "Fixer Pro Plan",
                "price": 4.99,
                "return_url": return_url,
                "test": True
            }
        }

        if access_token and access_token.startswith("shpat_"):
            req = urllib.request.Request(
                f"https://{shop}/admin/api/2026-01/recurring_application_charges.json",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "X-Shopify-Access-Token": access_token,
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    res_data = json.loads(resp.read().decode("utf-8"))
                    conf_url = res_data["recurring_application_charge"]["confirmation_url"]
                    return redirect(conf_url)
            except Exception:
                pass

        return jsonify({
            "status": "billing_ready",
            "shop": shop,
            "price_usd": 4.99,
            "confirmation_url": return_url
        }), 200

    @bp.route("/billing/confirm", methods=["GET"])
    def confirm_upgrade():
        shop = request.args.get("shop")
        fixer_id = request.args.get("fixer_id")
        if not shop or not fixer_id:
            return jsonify({"error": "missing_parameters"}), 400

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT fixer_id FROM stores WHERE store_id = ?", (shop,))
            row = cur.fetchone()
            if not row or row[0] != fixer_id:
                return jsonify({"error": "unauthorized"}), 403
            cur.execute("UPDATE stores SET plan = 'pro' WHERE store_id = ?", (shop,))
            conn.commit()

        return redirect(f"/api/fixer/dashboard?shop={shop}&fixer_id={fixer_id}")
