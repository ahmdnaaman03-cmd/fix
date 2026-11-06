import os
import sys
from flask import Flask, Blueprint, jsonify

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from db import DB_PATH, FREE_TIER_LIMIT, SHOPIFY_API_SECRET, init_db
from utils import verify_shopify_hmac, validate_and_format_phone
from oauth import register_oauth
from routes import register_routes
from webhooks import register_webhooks
from billing import register_billing

fixer_bp = Blueprint("fixer", __name__, url_prefix="/api/fixer")

register_oauth(fixer_bp)
register_routes(fixer_bp)
register_webhooks(fixer_bp)
register_billing(fixer_bp)

def create_app():
    app = Flask(__name__)
    init_db()
    app.register_blueprint(fixer_bp)

    @app.route("/", methods=["GET"])
    def home():
        return jsonify({"service": "Fixer Order & Phone Validator", "status": "running"}), 200

    return app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
