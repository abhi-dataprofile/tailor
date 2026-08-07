"""billing.py — subscriptions/plans with provider fallback.

Primary: Stripe (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_*).
Fallback: LemonSqueezy (LEMONSQUEEZY_API_KEY, LEMONSQUEEZY_STORE_ID, LEMONSQUEEZY_VARIANT_*)
          — a merchant-of-record option (handles global tax) that slots in when Stripe isn't set.

Both create a hosted checkout URL and report plan changes via webhook. All stdlib.
Never handles card data — that lives on the provider's hosted page.
"""
import os, json, hmac, hashlib, time, urllib.request, urllib.parse, urllib.error

STRIPE_KEY   = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WHSEC = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
LS_KEY       = os.environ.get("LEMONSQUEEZY_API_KEY", "")
LS_STORE     = os.environ.get("LEMONSQUEEZY_STORE_ID", "")

# map your plan name -> provider price/variant id via env, e.g. STRIPE_PRICE_PRO=price_123
def _price(plan, provider):
    return os.environ.get(f"{'STRIPE_PRICE' if provider=='stripe' else 'LEMONSQUEEZY_VARIANT'}_{plan.upper()}", "")

def provider():
    if STRIPE_KEY: return "stripe"
    if LS_KEY:     return "lemonsqueezy"
    return None

# ---------- Stripe ----------
def _stripe(path, data=None, method="POST"):
    url = "https://api.stripe.com/v1/" + path
    body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": "Bearer " + STRIPE_KEY,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def stripe_checkout(user_id, plan, success_url, cancel_url, email=None):
    price = _price(plan, "stripe")
    if not price:
        return {"ok": False, "detail": f"No STRIPE_PRICE_{plan.upper()} configured"}
    data = {"mode": "subscription", "line_items[0][price]": price, "line_items[0][quantity]": 1,
            "success_url": success_url, "cancel_url": cancel_url,
            "client_reference_id": user_id, "metadata[user_id]": user_id, "metadata[plan]": plan}
    if email: data["customer_email"] = email
    j = _stripe("checkout/sessions", data)
    return {"ok": True, "url": j.get("url"), "id": j.get("id")}

def stripe_verify(payload_bytes, sig_header):
    """Verify Stripe webhook signature. Returns the event dict or None."""
    if not STRIPE_WHSEC:
        return json.loads(payload_bytes)  # dev: accept unsigned (set the secret in prod)
    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(","))
        signed = parts["t"].encode() + b"." + payload_bytes
        expected = hmac.new(STRIPE_WHSEC.encode(), signed, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, parts["v1"]):
            return json.loads(payload_bytes)
    except Exception:
        return None
    return None

# ---------- LemonSqueezy (fallback) ----------
def _ls(path, data):
    url = "https://api.lemonsqueezy.com/v1/" + path
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST",
        headers={"Authorization": "Bearer " + LS_KEY, "Accept": "application/vnd.api+json",
                 "Content-Type": "application/vnd.api+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def ls_checkout(user_id, plan, success_url, cancel_url, email=None):
    variant = _price(plan, "lemonsqueezy")
    if not (variant and LS_STORE):
        return {"ok": False, "detail": "LemonSqueezy store/variant not configured"}
    payload = {"data": {"type": "checkouts",
        "attributes": {"checkout_data": {"custom": {"user_id": user_id, "plan": plan}},
                       "product_options": {"redirect_url": success_url}},
        "relationships": {"store": {"data": {"type": "stores", "id": LS_STORE}},
                          "variant": {"data": {"type": "variants", "id": variant}}}}}
    j = _ls("checkouts", payload)
    return {"ok": True, "url": (((j.get("data") or {}).get("attributes") or {}).get("url"))}

# ---------- unified ----------
def checkout(user_id, plan, success_url, cancel_url, email=None):
    p = provider()
    if p == "stripe":       return stripe_checkout(user_id, plan, success_url, cancel_url, email)
    if p == "lemonsqueezy": return ls_checkout(user_id, plan, success_url, cancel_url, email)
    return {"ok": False, "detail": "No billing provider configured"}

def parse_webhook(provider_name, payload_bytes, headers):
    """Return (user_id, plan, status) from a provider webhook, or (None, None, None)."""
    try:
        if provider_name == "stripe":
            ev = stripe_verify(payload_bytes, headers.get("Stripe-Signature", ""))
            if not ev: return (None, None, None)
            obj = (ev.get("data") or {}).get("object") or {}
            t = ev.get("type", "")
            uid = (obj.get("metadata") or {}).get("user_id") or obj.get("client_reference_id")
            if t == "checkout.session.completed":
                return (uid, (obj.get("metadata") or {}).get("plan", "pro"), "active")
            if t.startswith("customer.subscription"):
                return (uid, "pro", "active" if obj.get("status") == "active" else "canceled")
        else:  # lemonsqueezy
            ev = json.loads(payload_bytes)
            meta = (((ev.get("meta") or {}).get("custom_data")) or {})
            attr = (((ev.get("data") or {}).get("attributes")) or {})
            status = "active" if attr.get("status") in ("active", "paid", "on_trial") else "canceled"
            return (meta.get("user_id"), meta.get("plan", "pro"), status)
    except Exception:
        pass
    return (None, None, None)
