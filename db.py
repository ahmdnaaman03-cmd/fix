import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixer.db")
FREE_TIER_LIMIT = 50
SHOPIFY_API_KEY = os.environ.get("SHOPIFY_API_KEY", "")
SHOPIFY_API_SECRET = os.environ.get("SHOPIFY_API_SECRET", "")
APP_URL = os.environ.get("APP_URL", "https://Orderping.pythonanywhere.com")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stores (
                fixer_id TEXT PRIMARY KEY,
                store_id TEXT UNIQUE,
                access_token TEXT,
                refresh_token TEXT,
                plan TEXT DEFAULT 'free',
                orders_count INTEGER DEFAULT 0
            )
        """)
        for col in ("access_token", "refresh_token"):
            try:
                conn.execute(f"ALTER TABLE stores ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
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
