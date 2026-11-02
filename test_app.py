from app import app, init_db

init_db()
client = app.test_client()

test_cases = [
    {"id": "1001", "phone": "01066004512"},
    {"id": "1002", "phone": "00966501234567"},
    {"id": "1003", "phone": "12345"}
]

print("\n🚀 بدء اختبار محرك Fixer محلياً:\n")
for case in test_cases:
    res = client.post(
        "/api/fixer/webhook/order",
        json=case,
        headers={"X-Shopify-Shop-Domain": "test-store.myshopify.com"}
    )
    data = res.get_json()
    print(
        f"✅ طلب #{data['order_id']} | "
        f"قبل: {data['raw_phone']} -> بعد: {data['clean_phone']} | "
        f"الحالة: {data['validation_status']} | "
        f"الاستهلاك: {data['orders_used']}/{data['free_limit']}"
    )
