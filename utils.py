import re
import hmac
import hashlib
import base64
from db import SHOPIFY_API_SECRET

ARABIC_DIGITS_MAP = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789"
)

COUNTRY_CODES = {
    "EG": "+20",
    "SA": "+966",
    "AE": "+971",
    "KW": "+965",
    "QA": "+974",
    "BH": "+973",
    "OM": "+968",
    "JO": "+962"
}

def verify_shopify_hmac(raw_body, hmac_header):
    if not SHOPIFY_API_SECRET or not hmac_header:
        return True
    digest = hmac.new(SHOPIFY_API_SECRET.encode("utf-8"), raw_body, hashlib.sha256).digest()
    computed_hmac = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed_hmac, hmac_header)

def verify_oauth_hmac(params):
    if not SHOPIFY_API_SECRET:
        return True
    hmac_val = params.get("hmac")
    if not hmac_val:
        return False
    sorted_params = "&".join(
        f"{k}={v}" for k, v in sorted(params.items()) if k != "hmac"
    )
    digest = hmac.new(
        SHOPIFY_API_SECRET.encode("utf-8"),
        sorted_params.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, hmac_val)

def extract_order_phone(data):
    shipping = data.get("shipping_address") or {}
    billing = data.get("billing_address") or {}
    customer = data.get("customer") or {}

    raw_phone = (
        shipping.get("phone")
        or billing.get("phone")
        or data.get("phone")
        or customer.get("phone")
        or ""
    )
    country_code_iso = (
        shipping.get("country_code")
        or billing.get("country_code")
        or "EG"
    )
    dial_code = COUNTRY_CODES.get(str(country_code_iso).upper(), "+20")
    return raw_phone, dial_code

def validate_and_format_phone(raw_phone, default_country_code="+20"):
    if not raw_phone:
        return None, "missing_phone"
    normalized_input = str(raw_phone).translate(ARABIC_DIGITS_MAP)
    digits = re.sub(r"[^\d+]", "", normalized_input)

    if digits.startswith("00"):
        digits = "+" + digits[2:]

    if default_country_code == "+20" and not digits.startswith("+20"):
        m_eg = re.search(r"0?(1[0125]\d{8,})$", digits)
        if m_eg:
            digits = "+20" + m_eg.group(1)

    elif default_country_code in ("+966", "+971") and not digits.startswith(default_country_code):
        m_gcc = re.search(r"0?(5\d{8,})$", digits)
        if m_gcc:
            digits = default_country_code + m_gcc.group(1)

    elif digits.startswith("0") and not digits.startswith("00"):
        digits = default_country_code + digits[1:]
    elif not digits.startswith("+"):
        digits = default_country_code + digits

    if default_country_code == "+20" or digits.startswith("+20"):
        if re.match(r"^\+201[0125]\d{8}$", digits):
            return digits, "valid"
        return digits, "invalid_format"

    if default_country_code in ("+966", "+971") or digits.startswith(("+966", "+971")):
        if re.match(r"^\+(966|971)5\d{8}$", digits):
            return digits, "valid"
        return digits, "invalid_format"

    if re.match(r"^\+\d{10,15}$", digits):
        return digits, "valid"
    return digits, "invalid_format"
