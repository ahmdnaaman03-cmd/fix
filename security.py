import os
import re
import hmac
import json
import time
import base64
import hashlib
import secrets
import db

SHOP_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")

def is_valid_shop(shop):
    return bool(isinstance(shop, str) and SHOP_REGEX.match(shop))

def generate_oauth_state(prefix=""):
    return f"{prefix}{secrets.token_hex(16)}"

def _get_secret(secret=None):
    if secret is not None:
        return str(secret).strip()
    return (os.environ.get("SHOPIFY_API_SECRET") or db.SHOPIFY_API_SECRET or "").strip()

def _get_api_key(api_key=None):
    if api_key is not None:
        return str(api_key).strip()
    return (os.environ.get("SHOPIFY_API_KEY") or db.SHOPIFY_API_KEY or "").strip()

def generate_csrf_token(shop, fixer_id, secret=None):
    key = _get_secret(secret) or "fixer-local-csrf-key"
    msg = f"csrf:{shop}:{fixer_id}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()

def verify_csrf_token(shop, fixer_id, token, secret=None):
    if not token or not isinstance(token, str):
        return False
    expected = generate_csrf_token(shop, fixer_id, secret=secret)
    return hmac.compare_digest(expected, token)

def _b64url_decode(data):
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)

def _b64url_encode(raw_bytes):
    return base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("utf-8")

def verify_shopify_jwt(token, expected_shop=None, secret=None, api_key=None):
    signing_secret = _get_secret(secret)
    expected_aud = _get_api_key(api_key)
    if not token or not signing_secret:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        if header.get("alg") != "HS256":
            return None
        signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
        expected_sig = _b64url_encode(
            hmac.new(signing_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected_sig, sig_b64):
            return None

        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        now = time.time()
        exp = payload.get("exp")
        if exp is None or now > float(exp) + 5:
            return None
        nbf = payload.get("nbf")
        if nbf is not None and now + 5 < float(nbf):
            return None
        if expected_aud and payload.get("aud") != expected_aud:
            return None

        dest = payload.get("dest", "")
        dest_shop = dest.replace("https://", "").replace("http://", "").split("/")[0]
        if not is_valid_shop(dest_shop):
            return None
        if expected_shop and dest_shop != expected_shop:
            return None
        return payload
    except Exception:
        return None
