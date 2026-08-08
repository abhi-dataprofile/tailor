"""app_status.py — the single source of truth for an application's lifecycle status.

Both the interactive endpoints (serve.py) and the background auto-runner (apply.py)
classify raw engine results through here, so "what does 'submitted' mean" is defined
in exactly one place.

The engine result may carry:
  sent      : bool  — we actually clicked the submit button (form left our hands)
  confirmed : bool  — we SAW proof it was received (success page; email adds more later)
  status    : str   — the engine's raw code (captcha, unconfirmed, http_500, …)
"""

# statuses that mean "don't attempt this job again" — terminal, or waiting on a human.
# submitted_unconfirmed is settled too: it was SENT; we confirm it, we never resend.
SETTLED = ("confirmed", "submitted_unconfirmed", "blocked_captcha", "failed_permanent", "needs_you")

# human-readable labels + a coarse tone for the UI badge
LABELS = {
    "draft":                 ("Draft",              "neutral"),
    "queued":                ("In queue",           "neutral"),
    "filling":               ("Agent working…",     "info"),
    "needs_you":             ("Needs your answer",  "warn"),
    "blocked_captcha":       ("Captcha — finish it","warn"),
    "submitted_unconfirmed": ("Sent — confirming",  "info"),
    "confirmed":             ("Applied",            "good"),
    "failed_transient":      ("Retrying",           "warn"),
    "failed_permanent":      ("Couldn't apply",     "bad"),
    # legacy statuses (pre-migration rows) so the Activity feed still renders them:
    "submitted":             ("Sent — confirming",  "info"),
    "manual":                ("Needs a human",      "warn"),
    "awaiting_review":       ("Needs your answer",  "warn"),
    "approved":              ("Applied",            "good"),
    "failed":                ("Failed",             "bad"),
}


def classify(res):
    """Raw engine result dict → honest lifecycle status string."""
    res = res or {}
    raw = (res.get("status") or "").lower()
    if raw in ("dry_run", "dry_prepared"):
        return "draft"
    # "confirmed" is EARNED — only an explicit confirmed flag (a success page we
    # actually saw, or a matched confirmation email) counts. A raw "submitted" from
    # an HTTP code is NOT proof, so it is honestly "sent, not yet confirmed".
    if res.get("confirmed"):
        return "confirmed"
    if res.get("sent") or raw in ("submitted", "unconfirmed"):
        return "submitted_unconfirmed"
    if raw == "captcha":
        return "blocked_captcha"
    if raw in ("needs_review", "needs_answers"):
        return "needs_you"
    if raw in ("unsupported", "unsupported_form", "no_submit_button", "blocked"):
        return "failed_permanent"
    return "failed_transient"   # http_*, network_error, browser_error, error, timeouts
