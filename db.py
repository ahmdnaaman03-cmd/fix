import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixer.db")
FREE_TIER_LIMIT = 50
SHOPIFY_API_KEY = os.environ.get("SHOPIFY_API_KEY", "")
SHOPIFY_API_SECRET = os.environ.get("SHOPIFY_API_SECRET", "")
APP_URL = os.environ.get("FIXER_APP_URL", "https://Orderping.pythonanywhere.com")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stores (
                fixer_id TEXT PRIMARY KEY,
                store_id TEXT UNIQUE,
                access_token TEXT,
                plan TEXT DEFAULT 'free',
                orders_count INTEGER DEFAULT 0
            )
        """)
        try:
            conn.execute("ALTER TABLE stores ADD COLUMN access_token TEXT")
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
