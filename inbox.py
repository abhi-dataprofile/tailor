"""inbox.py — inbound email parsing + OTP extraction, provider-agnostic.

Give each user a unique address like  <userid>@inbox.<your-domain>  and point an
inbound email provider's webhook at /api/inbox/inbound. This module normalizes the
payload (Cloudflare Email Worker / SendGrid Inbound Parse / Postmark / generic JSON),
extracts the recipient user_id and any one-time code, so the apply flow can autofill it.

Pure stdlib. No provider lock-in — normalize() sniffs the shape.
"""
import re, json

# 4–8 digit codes, favouring ones near verification wording
_CODE_NEAR = re.compile(r"(?:code|otp|one[- ]?time|verification|passcode|pin)[^0-9]{0,40}(\d{4,8})", re.I)
_CODE_ANY  = re.compile(r"\b(\d{6})\b")

def extract_otp(text):
    if not text:
        return None
    m = _CODE_NEAR.search(text)
    if m:
        return m.group(1)
    m = _CODE_ANY.search(text)       # fall back to any standalone 6-digit
    return m.group(1) if m else None

def _user_from_recipient(addr, domain_suffix=None):
    """`abhishek+abc123@inbox.you.com` or `abc123@inbox.you.com` -> user id 'abc123'."""
    if not addr:
        return None
    local = addr.split("@", 1)[0]
    if "+" in local:
        return local.split("+", 1)[1]
    return local

def normalize(payload, headers=None):
    """Return {user_id, from_addr, to_addr, subject, body, otp} from any provider shape."""
    p = payload or {}
    # provider sniffing
    to = (p.get("to") or p.get("recipient") or p.get("To") or
          (p.get("envelope") or {}).get("to") or "")
    if isinstance(to, list):
        to = to[0] if to else ""
    frm = (p.get("from") or p.get("sender") or p.get("From") or (p.get("headers") or {}).get("from") or "")
    subject = p.get("subject") or p.get("Subject") or ""
    body = (p.get("text") or p.get("plain") or p.get("TextBody") or p.get("body-plain") or
            p.get("html") or p.get("HtmlBody") or p.get("body") or "")
    if isinstance(body, dict):
        body = body.get("text") or body.get("html") or ""
    body = re.sub(r"<[^>]+>", " ", str(body))
    to = to if isinstance(to, str) else str(to)
    return {"user_id": _user_from_recipient(to), "from_addr": str(frm)[:200],
            "to_addr": to[:200], "subject": str(subject)[:300],
            "body": body[:5000], "otp": extract_otp(subject + " " + body)}
