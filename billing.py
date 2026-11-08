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
  <div style="max-width:800px; margin:auto; background:#fff; padding:24px; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,0.1);">
    <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #e1e3e5; padding-bottom:16px; margin-bottom:16px;">
      <div>
        <h2 style="margin:0;">محرك Fixer لتصحيح أرقام الطلبات وحماية الشحن</h2>
        <p style="margin:6px 0 0; color:#6d7175;">المتجر المتصل: <strong>{{ store_id }}</strong></p>
      </div>
      <div>
        {% if plan == 'free' %}
          <a href="/api/fixer/billing/upgrade?shop={{ store_id }}&fixer_id={{ fixer_id }}"
             style="display:inline-block; padding:10px 18px; background:#008060; color:#fff; text-decoration:none; border-radius:6px; font-weight:bold;">
             الترقية للباقة غير المحدودة ($4.99/شهرياً)
          </a>
        {% else %}
          <span style="background:#e3f1df; color:#008060; padding:8px 14px; border-radius:6px; font-weight:bold;">باقة Pro غير المحدودة مفعلة</span>
        {% endif %}
      </div>
    </div>

    <div style="display:flex; gap:16px; margin-bottom:24px;">
      <div style="flex:1; background:#f9fafb; padding:14px; border-radius:6px; border:1px solid #e1e3e5;">
        <div style="color:#6d7175; font-size:13px;">الباقة الحالية</div>
        <div style="font-size:18px; font-weight:bold; margin-top:4px;">{{ 'مجانية (Free)' if plan == 'free' else 'احترافية (Pro)' }}</div>
      </div>
      <div style="flex:1; background:#f9fafb; padding:14px; border-radius:6px; border:1px solid #e1e3e5;">
        <div style="color:#6d7175; font-size:13px;">الطلبات المعالجة</div>
        <div style="font-size:18px; font-weight:bold; margin-top:4px;">{{ orders_used }} {% if plan == 'free' %}من {{ free_limit }}{% endif %}</div>
      </div>
    </div>

    <h3 style="margin-bottom:12px;">سجل التدخل التلقائي وتصحيح أرقام العملاء (E.164)</h3>
    {% if logs %}
      <table style="width:100%; border-collapse:collapse; font-size:14px; text-align:right;">
        <thead>
          <tr style="background:#f1f2f3; border-bottom:2px solid #dfe3e8;">
            <th style="padding:10px;">رقم الطلب</th>
            <th style="padding:10px;">الرقم المدخل (قبل)</th>
            <th style="padding:10px;">الرقم الدولي المعتمد (بعد)</th>
            <th style="padding:10px;">إجراء النظام في شوبيفاي</th>
          </tr>
        </thead>
        <tbody>
          {% for log in logs %}
          <tr style="border-bottom:1px solid #e1e3e5;">
            <td style="padding:10px; font-weight:bold;">#{{ log[0] }}</td>
            <td style="padding:10px; color:#6d7175; direction:ltr; text-align:right;">{{ log[1] or 'بدون رقم' }}</td>
            <td style="padding:10px; font-weight:bold; color:#008060; direction:ltr; text-align:right;">{{ log[2] or '-' }}</td>
            <td style="padding:10px;">
              {% if log[3] == 'valid' %}
                <span style="background:#e3f1df; color:#008060; padding:4px 8px; border-radius:4px; font-size:12px;">✅ تم التحديث ووسم E.164 Verified</span>
              {% else %}
                <span style="background:#fbeae5; color:#d82c0d; padding:4px 8px; border-radius:4px; font-size:12px;">⚠️ وسم تحذير Invalid Phone لمنع الشحن الخاطئ</span>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p style="background:#f9fafb; padding:16px; border-radius:6px; color:#6d7175; text-align:center;">
        لا توجد طلبات مسجلة حتى الآن. بمجرد دخول أي طلب جديد لمتجرك، سيقوم التطبيق بفحص وتصحيح رقم الهاتف تلقائياً هنا.
      </p>
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
            cur.execute(
                "SELECT order_id, raw_phone, clean_phone, status FROM order_logs WHERE store_id = ? ORDER BY id DESC LIMIT 20",
                (shop,)
            )
            logs = cur.fetchall()
            return render_template_string(
                DASHBOARD_HTML,
                fixer_id=row[0],
                store_id=shop,
                plan=row[1],
                orders_used=row[2],
                free_limit=FREE_TIER_LIMIT,
                logs=logs
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
