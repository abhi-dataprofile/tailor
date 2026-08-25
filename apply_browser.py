#!/usr/bin/env python3
"""apply_browser.py — headless-browser application submitter (Playwright).

Drives the *real* apply form the way a person would: fills the standard fields,
uploads a real PDF résumé (rendered by the same browser), answers visible required
questions from the answers dict, and submits. This is the general-purpose backend
for boards with no official API.

Honest boundaries (deliberate):
  * CAPTCHA / bot-check detected  -> ABORT to 'captcha' (manual). This does NOT
    solve or bypass CAPTCHAs — that's a line we don't cross.
  * No stealth / anti-detection tooling. If a board blocks automation, it returns
    'blocked' and the job is queued manual.
  * Never clicks submit unless called with dry=False (and the operator set
    APPLY_LIVE=1). Default is a safe dry prepare + screenshot.

Requires (optional dependency):
    pip install playwright && playwright install chromium

Test safely (prepares + screenshots, never submits):
    python3 apply_browser.py --url "https://job-boards.greenhouse.io/acme/jobs/123"
"""
import os, re, time, tempfile, argparse, datetime, json

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "applications_out")
# a dedicated, persistent real-Chrome profile — keeps cookies/session warm across applies
# (a genuine browser environment → fewer bot challenges; NOT fingerprint spoofing)
_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tailor_chrome")

RESUME_DIR = os.path.join(OUT_DIR, "resumes")

def _resolve_resume(r, timeout=90):
    """Accept the résumé as a string OR as something still being produced (a Future, or any
    zero-arg callable). Building it concurrently with page navigation hides most of the
    tailoring latency; this is where the two rejoin."""
    if r is None or isinstance(r, str):
        return r or ""
    try:
        if hasattr(r, "result"):          # concurrent.futures.Future
            return r.result(timeout=timeout) or ""
        if callable(r):
            return r() or ""
    except Exception as e:
        print("  [resume] not ready:", str(e)[:100])
    return ""

CAPTCHA_HINTS = ["recaptcha", "hcaptcha", "g-recaptcha", "cf-turnstile", "data-sitekey", "are you human",
                 "datadome", "captcha-delivery", "perimeterx", "px-captcha", "incapsula",
                 "verify you are human", "unusual traffic"]
# bot-check services that render in their own iframe — the host page then looks simply "empty",
# which reads as "no form here" unless we look at the frame URLs too.
_CAPTCHA_HOSTS = ("captcha-delivery.com", "datadome", "hcaptcha.com", "recaptcha.net",
                  "google.com/recaptcha", "challenges.cloudflare.com", "perimeterx.net")

# Per-vendor selector packs. Forms differ a lot between ATSes (and drift over time),
# so each vendor has its own field/submit selectors, with a generic fallback appended.
# These are best-effort and WILL need live tuning — treat them as a starting point.
_GENERIC = {
    "first_name": ["input[autocomplete='given-name']", "input[name*='first' i]"],
    "last_name":  ["input[autocomplete='family-name']", "input[name*='last' i]"],
    "full_name":  ["input[autocomplete='name']", "input[name='name']", "input[name*='full' i]"],
    "email":      ["input[type='email']", "input[name*='email' i]", "input[autocomplete='email']"],
    "phone":      ["input[type='tel']", "input[name*='phone' i]", "input[autocomplete='tel']"],
    "file":       ["input[type='file']"],
    "submit":     ["button[type='submit']", "button:has-text('Submit')", "button:has-text('Apply')", "input[type='submit']"],
}
VENDOR_PACKS = {
    "greenhouse": {                       # job-boards.greenhouse.io (modern) + boards.greenhouse.io
        "first_name": ["#first_name", "input[name='first_name']"],
        "last_name":  ["#last_name", "input[name='last_name']"],
        "email":      ["#email", "input[name='email']"],
        "phone":      ["#phone", "input[name='phone']"],
        "file":       ["input[type='file'][id*='resume' i]", "input[type='file']"],
        "submit":     ["#submit_app", "button:has-text('Submit Application')", "button[type='submit']"],
    },
    "lever": {                            # jobs.lever.co/<co>/<id>/apply — uses ONE full-name field
        "full_name":  ["input[name='name']"],
        "email":      ["input[name='email']"],
        "phone":      ["input[name='phone']"],
        "file":       ["input[name='resume']", "input[type='file']"],
        "submit":     ["button:has-text('Submit application')", "button[type='submit']"],
    },
    "ashby": {                            # jobs.ashbyhq.com — React form, label-driven
        "first_name": ["input[name*='first' i]"],
        "last_name":  ["input[name*='last' i]"],
        "full_name":  ["#_systemfield_name", "input[name='_systemfield_name']", "input[aria-label*='Name' i]"],
        "email":      ["#_systemfield_email", "input[type='email']"],
        "phone":      ["input[type='tel']"],
        "file":       ["input[type='file']"],
        "submit":     ["button:has-text('Submit Application')", "button:has-text('Submit')", "button[type='submit']"],
    },
    "smartrecruiters": {                  # jobs.smartrecruiters.com — click Apply reveals the form
        "first_name": ["input[name*='firstName' i]", "#firstName", "input[name*='first' i]"],
        "last_name":  ["input[name*='lastName' i]", "#lastName", "input[name*='last' i]"],
        "email":      ["input[name*='email' i]", "input[type='email']"],
        "phone":      ["input[name*='phone' i]", "input[type='tel']"],
        "file":       ["input[type='file']"],
        "submit":     ["button:has-text('Submit')", "button:has-text('Apply')", "button[type='submit']"],
    },
    "recruitee": {                        # careers.<co>.com — Recruitee-hosted/embedded form
        "first_name": ["input[name*='first' i]"],
        "last_name":  ["input[name*='last' i]"],
        "email":      ["input[type='email']", "input[name*='email' i]"],
        "phone":      ["input[type='tel']", "input[name*='phone' i]"],
        "file":       ["input[type='file']"],
        "submit":     ["button:has-text('Apply')", "button:has-text('Submit')", "button[type='submit']"],
    },
    "icims": {                            # *.icims.com — form usually inside an iframe (see _form_frame)
        "first_name": ["input[name*='firstname' i]", "#firstname", "input[id*='first' i]", "input[name*='first' i]"],
        "last_name":  ["input[name*='lastname' i]", "#lastname", "input[id*='last' i]", "input[name*='last' i]"],
        "email":      ["input[name*='email' i]", "input[type='email']", "input[id*='email' i]"],
        "phone":      ["input[name*='phone' i]", "input[type='tel']", "input[id*='phone' i]"],
        "file":       ["input[type='file']"],
        "submit":     ["button:has-text('Submit')", "input[type='submit']", "button:has-text('Continue')",
                       "button:has-text('Next')", "a:has-text('Submit')", "#quApply", "button[id*='submit' i]"],
    },
}

# apply-form context: the main page, OR an embedded ATS iframe (iCIMS and many corporate
# career pages load the real form inside an <iframe> the top document can't see into).
_EMAIL_SEL = "input[type='email'], input[name*='email' i], input[autocomplete='email']"

def _form_frame(page):
    if page.query_selector(_EMAIL_SEL):
        return page
    for fr in page.frames:
        try:
            if fr == page.main_frame:
                continue
            if fr.query_selector(_EMAIL_SEL):
                return fr
            u = (fr.url or "").lower()
            if any(k in u for k in ("icims", "greenhouse", "lever", "ashby")) and fr.query_selector("input,form"):
                return fr
        except Exception:
            continue
    return page

# consent-banner button text, most privacy-preserving first. We only need the form clickable —
# there's no reason to opt the candidate into tracking to apply for a job.
_CONSENT_TEXT = (
    (r"^(reject|decline)\s*(all|non[- ]?essential|optional)?$", r"^(only|accept)?\s*(strictly\s*)?"
     r"(necessary|essential)\s*(cookies|only)?$", r"^reject non-essential$", r"^continue without"),
    (r"^(dismiss|got it|close)$",),
    (r"^(accept|allow)\s*(all|cookies)?$", r"^i agree$", r"^ok$"),
)

def _dismiss_consent(page):
    """Clear a cookie/consent overlay, declining non-essential cookies where offered.

    Scans the page AND its iframes (consent platforms usually render in one), and matches on
    the element's own text rather than assuming a <button> tag — banners use <a> and <div
    role=button> just as often, which is why selector-only matching missed real ones."""
    for group in _CONSENT_TEXT:
        for pat in group:
            rx = re.compile(pat, re.I)
            for fr in [page] + list(getattr(page, "frames", []) or []):
                try:
                    els = fr.query_selector_all("button, a, [role=button], input[type=button], input[type=submit]")
                except Exception:
                    continue
                for el in els:
                    try:
                        if not el.is_visible():
                            continue
                        txt = (el.inner_text() or el.evaluate("e=>e.value||''") or "").strip()
                        if txt and len(txt) < 40 and rx.match(txt):
                            el.click(timeout=3000); page.wait_for_timeout(600)
                            return True
                    except Exception:
                        continue
    return False

def _clickable_by_text(scope, patterns):
    """Find a visible clickable element whose own text matches one of `patterns`.

    Matching happens in JS on the element's text, NOT via a CSS :has-text('...') selector —
    an apostrophe in the label ("I'm interested", the real apply button on SmartRecruiters)
    produces a malformed selector that throws and gets silently swallowed, which is why those
    pages looked like they had no apply button at all."""
    try:
        return scope.evaluate_handle(
            """(pats)=>{
              const rx = pats.map(p=>new RegExp(p,'i'));
              const els=[...document.querySelectorAll("a,button,[role=button],input[type=submit],input[type=button]")];
              for(const r of rx){
                for(const e of els){
                  const t=((e.innerText||e.value||'')+'').replace(/[\\s]+/g,' ').trim();
                  if(!t||t.length>40||!r.test(t)) continue;
                  const b=e.getBoundingClientRect();
                  if(b.width<2||b.height<2) continue;
                  const st=getComputedStyle(e);
                  if(st.visibility==='hidden'||st.display==='none') continue;
                  return e;
                }
              }
              return null;}""", patterns).as_element()
    except Exception:
        return None

# what the "take me to the application" control is called, most specific first
_APPLY_PATTERNS = [
    r"^apply for this (job|position|role)", r"^apply (now|online|here)", r"^start (your )?application",
    r"^(i'?m|i am) interested", r"^apply to this", r"^submit (an )?application", r"^continue to apply",
    r"^apply$", r"^apply with", r"^begin application", r"^application$",
]

def _has_form(page):
    """True once a real application form is reachable — an email field anywhere on the page
    or in any frame."""
    for scope in [page] + [fr for fr in page.frames if fr != page.main_frame]:
        try:
            if scope.query_selector(_EMAIL_SEL):
                return True
        except Exception:
            continue
    return False

def _reveal_apply(page, max_steps=3):
    """Navigate from wherever we landed to the actual application form.

    A job link often lands on a description page (or a careers-site wrapper) whose apply
    control leads to the real form — sometimes through more than one hop, sometimes in a new
    tab. Click through up to `max_steps` of those, waiting for the form (or a navigation)
    after each. Returns the page holding the form, which may be a NEW tab — callers must use
    the returned page, not the one they passed in."""
    ctx = page.context
    for _ in range(max_steps):
        if _has_form(page):
            return page
        btn = None
        for scope in [page] + [fr for fr in page.frames if fr != page.main_frame]:
            btn = _clickable_by_text(scope, _APPLY_PATTERNS)
            if btn:
                break
        if not btn:
            break
        before = set(ctx.pages)
        try:
            btn.click(timeout=5000)
        except Exception:
            try:
                btn.evaluate("e=>e.click()")     # covers overlay-intercepted clicks
            except Exception:
                break
        page.wait_for_timeout(1200)
        opened = [p for p in ctx.pages if p not in before]
        if opened:                                # the control opened the form in a new tab
            page = opened[-1]
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
        else:
            try:                                  # or navigated / rendered in place
                page.wait_for_selector(_EMAIL_SEL, timeout=8000)
            except Exception:
                page.wait_for_timeout(1200)
    return page

def _vendor_of(url):
    u = (url or "").lower()
    if "lever.co" in u: return "lever"
    if "ashbyhq" in u: return "ashby"
    if "greenhouse" in u: return "greenhouse"
    if "icims" in u: return "icims"
    if "smartrecruiters" in u: return "smartrecruiters"
    if "recruitee" in u: return "recruitee"
    return "generic"

def _pack(url):
    """Vendor selectors first, generic appended as fallback."""
    p = dict(_GENERIC)
    for k, v in VENDOR_PACKS.get(_vendor_of(url), {}).items():
        p[k] = v + _GENERIC.get(k, [])
    return p

def _pw():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception as e:
        raise RuntimeError("Playwright not installed — `pip install playwright && playwright install chromium`") from e

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

def _clean_contact(kind, value):
    """Normalise a contact value, and refuse to type one that is plainly invalid.

    A profile holding "abhicjadhav @gmail.com" (a stray space) was typed verbatim, and every
    board rejected it with "enter a valid email address" — so the application could never
    submit, on any board, for any job. Whitespace is stripped; if the result still isn't a
    valid address we don't type it, so the reason surfaces here instead of as a validation
    failure on the far side."""
    v = re.sub(r"\s+", "", str(value or "")) if kind in ("email", "phone") else str(value or "").strip()
    if kind == "email" and v and not _EMAIL_RE.match(v):
        print(f"  [contact] refusing to fill an invalid email: {value!r}")
        return ""
    return v

def _fill_first(page, selectors, value):
    if not value:
        return False
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(str(value)); return True
        except Exception:
            continue
    return False

def _captcha_blocking(page):
    """A CAPTCHA that is actually IN THE WAY — not merely present.

    Most ATS forms (Greenhouse, Lever, Ashby) ship an invisible reCAPTCHA that sits quietly
    beside a perfectly fillable form. Treating its presence as a block reported every working
    board as captcha-walled. A real wall means: a visibly rendered challenge frame, or a
    challenge with no application form anywhere to fill."""
    big_challenge = False
    try:
        for fr in page.frames:
            if not any(h in (fr.url or "").lower() for h in _CAPTCHA_HOSTS):
                continue
            el = fr.frame_element()
            try:
                if el and el.is_visible():
                    box = el.bounding_box() or {}
                    if (box.get("width") or 0) > 180 and (box.get("height") or 0) > 120:
                        big_challenge = True      # an actual challenge is rendered on screen
                        break
            except Exception:
                continue
    except Exception:
        pass
    if big_challenge:
        return True
    # no form to fill anywhere + a challenge in the markup = we're walled out
    try:
        has_fields = bool(page.query_selector(_EMAIL_SEL)) or any(
            fr.query_selector(_EMAIL_SEL) for fr in page.frames if fr != page.main_frame)
    except Exception:
        has_fields = False
    if has_fields:
        return False
    try:
        html = (page.content() or "").lower()
    except Exception:
        return False
    return any(h in html for h in ("captcha-delivery", "datadome", "perimeterx", "px-captcha",
                                   "are you human", "verify you are human", "unusual traffic",
                                   "challenges.cloudflare.com"))

def _render_pdf(context, resume_html, path):
    pg = context.new_page()
    pg.set_content(resume_html or "<html><body><p>Résumé</p></body></html>", wait_until="load")
    pg.pdf(path=path, format="Letter", margin={"top": "0.6in", "bottom": "0.6in", "left": "0.6in", "right": "0.6in"})
    pg.close()


# ---- answer bank: map a question's label text -> a value from the user's standing answers ----
ANSWER_KEYS = [
 ("first_name", ["first name","given name","legal first"]),
 ("last_name", ["last name","family name","surname","legal last"]),
 ("full_name", ["full name","legal name","your name"]),
 ("email", ["email"]),
 ("phone", ["phone","mobile number","contact number"]),
 ("current_location", ["current location","where are you located","currently located","where do you live","based in","city, state","city and state","location (city","your location","where are you based"]),
 ("current_company", ["current company","current employer","previous employer","most recent employer"]),
 ("current_title", ["current or previous job title","current job title","most recent title","current title","your job title","job title","current role"]),
 ("linkedin", ["linkedin"]),
 ("github", ["github"]),
 ("portfolio", ["portfolio"]),
 ("website", ["personal website","other website","personal url","website"]),
 ("work_authorized", ["authorized to work","legally authorized","eligible to work","right to work","authorization to work","work authorization in"]),
 ("needs_sponsorship", ["require sponsorship","need sponsorship","visa sponsorship","sponsorship for employment","require the company to file","now or in the future require","require our company to file","require immigration","sponsor you","to sponsor","sponsor your","require us to sponsor","will you require"]),
 ("citizenship", ["citizenship","country of citizenship","citizen of"]),
 ("visa_type", ["type of visa","which visa","work permit","immigration status","current visa","visa status"]),
 ("work_auth_basis", ["authorization basis","basis for your work","work authorization type","authorization status"]),
 ("desired_location", ["where would you like to work","preferred location","which location","location(s) you anticipate","location you are applying","country or countries you anticipate"]),
 ("relocate", ["relocat"]),
 ("remote_ok", ["come into the office","work remotely","work from a remote","in-person","onsite","hybrid policy"]),
 ("start_date", ["when can you start","start date","available to start","notice period","how much notice"]),
 # ONLY general-experience questions. "how many years" alone matched "years of hands-on Book
 # Keeping experience" and answered it with the candidate's total years — claiming four years
 # of bookkeeping they have never done. A general fact must never answer a specific one.
 ("years_experience", ["years of experience in overall","years of overall experience",
                       "total years of experience","years of professional experience",
                       "how many years of experience do you have in overall",
                       "years of relevant experience","overall experience"]),
 ("salary_expectation", ["salary","compensation expectation","desired pay","expected compensation"]),
 ("over_18", ["over 18","at least 18","18 years of age"]),
 ("gender", ["gender"]),
 ("ethnicity", ["ethnicity","identify my ethnicity","hispanic","latino"]),
 ("veteran", ["veteran"]),
 ("disability", ["disab"]),
 ("how_heard", ["how did you hear","where did you hear","how did you find"]),
 # from the answer-bank questionnaire — one saved answer covers every rewording a board uses
 ("currently_working", ["are you currently working","are you currently employed","currently working",
                        "current employment status"]),
 ("notice_period", ["notice period","official notice","how much notice","when can you join",
                    "how soon can you join","how soon you can join","joining time","availability to join"]),
 ("reason_for_change", ["reason for job change","reason for looking","why are you looking",
                        "reason for change","why do you want to leave"]),
 ("current_compensation", ["current total compensation","current ctc","current salary",
                           "present ctc","current compensation","last drawn"]),
 ("salary_expectation", ["expected total compensation","expected ctc","expected salary",
                         "salary expectation","desired compensation","compensation expectation"]),
 ("shift_ok", ["shift timing","emea shift","us shift","night shift","rotational shift",
               "ready to work in.{0,20}shift"]),
 ("remote_ok", ["hybrid mode","days wfo","work from office","days in the office","onsite",
                "in-office","come into the office","work remotely"]),
 ("desired_location", ["preferred location","preferred work location","location preference"]),
 ("school", ["school","university","college","institution attended","most recent school"]),
 # residence COUNTRY — kept last so specific country phrasings (citizenship, "countries you
 # anticipate") match their own keys first; a bare "Country" field falls through to here.
 ("country", ["country where you","country you reside","country of residence","currently reside","which country","select the country","your country","country"]),
]
_FP_STOP = set((
    "a an the of to in on for and or is are do does did you your we our will would can could "
    "have has had be been being with this that these those please provide enter select choose "
    "any about at as it if not no yes require required currently company role position job "
    "application applicant candidate work working "
    "why what how when where which who whom many much are"   # interrogatives/fillers, not salient
).split())

def _fp(text):
    """Salient-token fingerprint of a question: lowercase, drop stopwords, light-stem — so
    reworded questions ('Why interested in this role?' / 'What interests you about the position?')
    collapse to overlapping token sets."""
    out = set()
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if len(w) < 3 or w in _FP_STOP:
            continue
        if w.endswith("ies") and len(w) > 4:   w = w[:-3] + "y"
        elif w.endswith("ing") and len(w) > 5: w = w[:-3]
        elif w.endswith("ed") and len(w) > 4:  w = w[:-2]
        elif w.endswith("es") and len(w) > 4:  w = w[:-2]
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 3: w = w[:-1]
        out.add(w)
    return out

# a question that names a specific domain must not be answered from a general fact
_SPECIFIC_YEARS = re.compile(
    r"years?\b[^?]{0,40}\b(?:of|in|on|with|working on)\b\s*(?!experience\b)(?!relevant\b)"
    r"(?!professional\b)(?!overall\b)(?!total\b)[a-z]", re.I)

def _answer_for(label, bank):
    low = (label or "").lower()
    if "year" in low and _SPECIFIC_YEARS.search(low):
        # "years of hands-on Book Keeping experience", "years working on Quickbooks, GAAP" —
        # only the candidate can say, and a general total would be a false claim.
        generic = re.search(r"\b(overall|in total|total|professional|relevant)\b", low)
        if not generic:
            return None
    for key, syns in ANSWER_KEYS:
        for sy in syns:
            if sy in low:
                v = (bank or {}).get(key)
                if v not in (None, ""):
                    return str(v)
    # previously-answered novel questions. Match by exact-ish prefix OR salient-token overlap,
    # so a reworded question on a different board still reuses the answer you already gave.
    custom = (bank or {}).get("_custom") or {}
    qf = _fp(label)
    for k, v in custom.items():
        if not (k and v):
            continue
        kl = k.lower()
        if kl[:40] in low or (low and low[:40] in kl):
            return str(v)
        kf = _fp(k)
        if kf and qf:
            shared = qf & kf
            ns, mn = len(shared), min(len(qf), len(kf))
            if ns >= 2 and ns >= 0.6 * mn:
                return str(v)
            # short questions: every salient token of the shorter side matches, and it's a
            # substantial word (avg length >= 5) — enough signal to reuse the answer.
            if ns >= 1 and ns == mn and mn <= 2 and sum(len(t) for t in shared) / ns >= 5:
                return str(v)
    return None

# leading-text patterns that are placeholders/hints, NOT real question labels.
# (no bare '/' — this is spliced into a JS regex literal /^(...)/i)
_PLACEHOLDERY = "type here|pick date|start typing|select|choose|search|e\\.g\\.|hello@|upload|attach|optional"

def _label_text(el):
    """Best-effort question label for a field. ATS forms (esp. Ashby/React) wrap the
    question text several hashed-class <div>s above the input, so we walk ancestors for a
    label/legend/labelly node before falling back to placeholder/name."""
    try:
        js = (
          "e=>{const clean=s=>(s||'').replace(/[*\\u2731]/g,'').replace(/\\s+/g,' ').trim();"
          "const bad=/^(" + _PLACEHOLDERY + ")/i;"
          "if(e.getAttribute('aria-label'))return clean(e.getAttribute('aria-label'));"
          "if(e.labels&&e.labels[0]&&e.labels[0].innerText.trim())return clean(e.labels[0].innerText.split('\\n')[0]);"
          "const lb=e.getAttribute('aria-labelledby');"
          "if(lb){const l=document.getElementById(lb);if(l&&l.innerText.trim())return clean(l.innerText.split('\\n')[0]);}"
          # walk up: a dedicated label/legend/title element inside a small-ish ancestor
          "let node=e.parentElement;"
          "for(let i=0;i<6&&node;i++,node=node.parentElement){"
          "  const lab=node.querySelector('label,legend,.application-label,[class*=\"label\" i],[class*=\"title\" i],[class*=\"question\" i]');"
          "  if(lab&&!lab.contains(e)&&lab.innerText.trim()){const t=clean(lab.innerText.split('\\n')[0]);if(t.length>2&&!bad.test(t))return t;}"
          "}"
          # else the leading text line of a compact ancestor (avoids grabbing a whole form)
          "node=e.parentElement;"
          "for(let i=0;i<6&&node;i++,node=node.parentElement){"
          "  const n=node.querySelectorAll('input,textarea,select').length; if(n>2) break;"
          "  const t=(node.innerText||'').trim(); if(!t) continue;"
          "  const first=clean(t.split('\\n')[0]);"
          "  if(first.length>3&&first.length<160&&!bad.test(first))return first;"
          "}"
          "if(e.placeholder&&e.placeholder.trim())return clean(e.placeholder);"
          "if(e.name)return clean(e.name.replace(/[_\\-]+/g,' ').replace(/\\b\\w/g,c=>c.toUpperCase()));"
          "return '';}"
        )
        return (el.evaluate(js) or "").strip()
    except Exception:
        return ""

def _is_required(el):
    """True only when the field is genuinely required. The asterisk/'required' check is
    scoped to the field's OWN label (not a big ancestor div), so one '*' somewhere on the
    page no longer marks every field required — which used to block valid submits."""
    try:
        return bool(el.evaluate("""e=>{
          if(e.required||e.getAttribute('aria-required')==='true')return true;
          const c=e.closest('.application-question,[class*=question],[class*=field],.form-group,fieldset,label,li');
          if(c){const lbl=c.querySelector('label,legend,.label,.application-label');
            const t=((lbl&&lbl.innerText)||'').slice(0,120);
            if(/[*\\u2731]/.test(t)||/\\brequired\\b/i.test(t))return true;}
          return false;}"""))
    except Exception:
        return False

def _select_by_text(sel, ans):
    try:
        sel.select_option(label=str(ans)); return True
    except Exception:
        pass
    try:
        val = sel.evaluate("(e,a)=>{a=(a||'').toLowerCase();for(const o of e.options){if(o.text&&o.text.toLowerCase().includes(a))return o.value;}return null;}", str(ans))
        if val is not None:
            sel.select_option(value=val); return True
    except Exception:
        pass
    return False

def _radio_label(r):
    try:
        return (r.evaluate("""e=>{
          if(e.id){const l=document.querySelector("label[for='"+e.id+"']");if(l)return l.innerText;}
          const l2=e.closest("label");if(l2)return l2.innerText;
          const p=e.parentElement;if(p&&p.innerText)return p.innerText;return e.value||'';}""") or "").strip()
    except Exception:
        return ""

def _group_label(r):
    try:
        return (r.evaluate("""e=>{const clean=s=>(s||'').replace(/[*\u2731]/g,'').trim();
          let q=e.closest('.application-question,fieldset,[role=radiogroup]');
          if(!q)q=e.closest('.field,.form-group,li');
          if(q){const lab=q.querySelector('.application-label,legend');
            if(lab&&lab.innerText.trim())return clean(lab.innerText.split('\\n')[0]);
            const t=(q.innerText||'').trim();if(t)return clean(t.split('\\n')[0]);}
          return '';}""") or "").strip()
    except Exception:
        return ""

# marketing / communications opt-ins — declining is the privacy-preserving default (matches the
# app's consent stance), so we don't block a submission on an optional promotional checkbox.
_OPTIN_RE = re.compile(
    r"opt[- ]?in|receive (marketing|promotional|sms|text|whatsapp|email|recruiting)|"
    r"subscribe|marketing communications|promotional (messages|emails)|text messages?|newsletter", re.I)
def _default_optout(label):
    return "No" if _OPTIN_RE.search(label or "") else None

def _country_eq(a, b):
    """True if two strings name the same country — so an answer of 'United States' matches an
    option rendered as 'US'/'USA', which plain substring matching misses."""
    try:
        import geo
        ca, cb = geo.country_of(a), geo.country_of(b)
        return bool(ca) and ca == cb
    except Exception:
        return False

_US_STATE = {
    "alabama":"al","alaska":"ak","arizona":"az","arkansas":"ar","california":"ca","colorado":"co",
    "connecticut":"ct","delaware":"de","florida":"fl","georgia":"ga","hawaii":"hi","idaho":"id",
    "illinois":"il","indiana":"in","iowa":"ia","kansas":"ks","kentucky":"ky","louisiana":"la",
    "maine":"me","maryland":"md","massachusetts":"ma","michigan":"mi","minnesota":"mn",
    "mississippi":"ms","missouri":"mo","montana":"mt","nebraska":"ne","nevada":"nv",
    "new hampshire":"nh","new jersey":"nj","new mexico":"nm","new york":"ny",
    "north carolina":"nc","north dakota":"nd","ohio":"oh","oklahoma":"ok","oregon":"or",
    "pennsylvania":"pa","rhode island":"ri","south carolina":"sc","south dakota":"sd",
    "tennessee":"tn","texas":"tx","utah":"ut","vermont":"vt","virginia":"va","washington":"wa",
    "west virginia":"wv","wisconsin":"wi","wyoming":"wy",
}

def _norm_place(v):
    """Collapse a place string to comparable tokens, folding state names to their codes so
    "Buffalo, New York" and "Buffalo, NY, USA" share the state token instead of looking like
    different cities."""
    v = str(v or "").lower()
    for name, code in _US_STATE.items():
        v = re.sub(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", code, v)
    return v

def _is_bare_country(v):
    """True when the whole string is just a country ("US", "USA", "United States") — not a
    city that merely happens to sit in one. Without this, country-equivalence made "San Jose,
    CA" and "Buffalo, Wyoming, USA" the same place, because both resolve to the US."""
    v = str(v or "").strip().strip(",.")
    if not v or len(v.split()) > 3:
        return False
    try:
        import geo
        c = geo.country_of(v)
        return bool(c) and (v.lower() == c.lower() or len(v) <= 3)
    except Exception:
        return False

def _opt_score(ans, text):
    """How well one option matches an answer, 0..1.

    The leading token carries most of the weight: for a place, that is the city name, which is
    what actually distinguishes "Buffalo, NY, USA" from "Buffalo City, Eastern Cape, South
    Africa". Extra words in the option are penalised lightly so a longer, noisier option does
    not win on overlap alone."""
    a, t = str(ans or "").strip().lower(), str(text or "").strip().lower()
    if not a or not t:
        return 0.0
    if a == t:
        return 1.0
    if _is_bare_country(a) and _country_eq(a, t):
        return 0.95
    aw = re.findall(r"[a-z0-9+]+", _norm_place(a))
    tw = re.findall(r"[a-z0-9+]+", _norm_place(t))
    if not aw or not tw:
        return 0.0
    sa, st = set(aw), set(tw)
    shared = sa & st
    if not shared:
        return 0.0
    # a short distinctive token that appears whole — a dialling code (+1), a state code (NY),
    # an airport-style abbreviation — identifies the option on its own
    if len(aw) == 1 and len(aw[0]) <= 4 and aw[0] in st:
        return 0.9
    lead = 0.60 if aw[0] == tw[0] else 0.0            # same first word → almost certainly it
    cover = 0.40 * (len(shared) / len(sa))            # how much of the answer is present
    noise = 0.20 * (len(st - sa) / max(1, len(st)))   # how much the option adds
    return max(0.0, lead + cover - noise)

_BOOLish = {"true": "yes", "false": "no", "y": "yes", "n": "no", "1": "yes", "0": "no"}

def _opt_match(ans, texts, threshold=0.34):
    """Index of the option that BEST matches `ans`, or None if nothing is close enough.

    Returning the first option that shared any text is how "Buffalo" became "Buffalo City,
    Eastern Cape, South Africa" on a live application."""
    a = str(ans or "").strip().lower()
    if not a:
        return None
    a = _BOOLish.get(a, a)               # a model may answer a Yes/No control with true/false
    # ties break toward the EARLIER option: autocompletes and selects list their most
    # relevant choice first, so position is real signal when scores are equal.
    scored = sorted(((_opt_score(a, t), -i) for i, t in enumerate(texts)), reverse=True)
    if scored and scored[0][0] >= threshold:
        return -scored[0][1]
    return None

_DATE_WORDS = re.compile(r"\b(date|start|available|availability|when can you (start|begin)|earliest|notice period)\b", re.I)

def _looks_date(label, el):
    if _DATE_WORDS.search(label or ""):
        return True
    ph = ""
    try: ph = (el.evaluate("e=>(e.placeholder||'')+' '+(e.type||'')") or "").lower()
    except Exception: pass
    return "date" in ph or "mm/dd" in ph or "dd/mm" in ph or " date" in ph

def _date_obj(ans):
    """Turn a start-date answer into a concrete date. Handles explicit dates and relative
    phrases ('2 weeks', 'immediately'). Returns a datetime.date, or None if there's no
    answer to work from (so the caller asks instead of inventing a commitment)."""
    s = (ans or "").strip()
    if not s:
        return None
    low = s.lower()
    today = datetime.date.today()
    if any(w in low for w in ("immediat", "asap", "right away", "now", "available now", "anytime")):
        return today
    m = re.search(r"(\d+)\s*(day|week|month)", low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return today + datetime.timedelta(days=n * (1 if unit == "day" else 7 if unit == "week" else 30))
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y", "%m/%d/%y", "%m/%d"):
        try:
            d = datetime.datetime.strptime(s, fmt).date()
            return d.replace(year=today.year) if d.year == 1900 else d
        except Exception:
            continue
    return today + datetime.timedelta(days=21)   # a present-but-unparseable answer → ~3 weeks out

def _fill_date(el, dobj):
    """Set a date field. Native <input type=date> wants ISO; custom text pickers (Ashby)
    accept a typed MM/DD/YYYY. Returns True if a value landed."""
    try:
        native = el.evaluate("e=>e.type") == "date"
    except Exception:
        native = False
    if native:
        try: el.fill(dobj.strftime("%Y-%m-%d")); return True
        except Exception: return False
    v = dobj.strftime("%m/%d/%Y")
    try:
        el.click(); el.fill(""); el.type(v, delay=25)
        try: el.press("Enter")
        except Exception: pass
        return bool(el.evaluate("e=>e.value"))
    except Exception:
        try: el.fill(v); return True
        except Exception: return False

# Questions we will NEVER let a model answer — legal / comp / demographic / eligibility.
# These come ONLY from the user's explicit standing answers (or they stay unfilled → needs_you).
_SENSITIVE_RE = re.compile(
    r"(sponsor|visa|authoriz\w* to work|work authoriz|right to work|citizen|immigration|"
    r"salary|compensation|expected pay|desired pay|felon|convict|criminal|background check|"
    r"disab|veteran|gender|\bsex\b|\brace\b|ethnic|hispanic|latino|orientation|"
    r"date of birth|birth date|\bdob\b|social security|\bssn\b|\bage\b|18 years|over 18)", re.I)

def _strip_html(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h or "")).strip()

# A model asked not to invent often answers with a SENTENCE meaning "I don't know" rather than
# an empty string. Typing "Not specified in the provided material" into a Location field is
# worse than leaving it blank, so these are treated as non-answers.
_REFUSAL_RE = re.compile(
    r"^\s*(n/?a|none|unknown|not\s+(specified|provided|mentioned|available|applicable|stated|"
    r"given|listed|indicated|determined|found|present|in\s+the)|no\s+(information|data|answer)|"
    r"cannot\s+(be\s+)?(determined|answer)|unable\s+to|i\s+(don'?t|do\s+not)\s+(know|have)|"
    r"insufficient|the\s+material\s+does\s+not)\b", re.I)

def _is_refusal(v):
    v = str(v or "").strip()
    return (not v) or bool(_REFUSAL_RE.match(v)) or len(v) > 400

def _answers_by_id(raw, labels):
    """Read {"answers":[{"id":1,"a":"…"}]} back onto the question labels.

    Asking for answers keyed by the question TEXT meant matching on whatever the model echoed
    — reworded, re-punctuated, snake_cased, or nested one level too high. An index is
    unambiguous, and a model is far more reliable at returning one entry per id than at
    reproducing long question strings."""
    if not raw:
        return {}
    m = re.search(r'"answers"\s*:\s*(\[[\s\S]*?\])', raw)
    if not m:
        return {}
    try:
        rows = json.loads(m.group(1))
    except Exception:
        return {}
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            i = int(r.get("id", 0)) - 1
        except Exception:
            continue
        v = r.get("a", r.get("answer", ""))
        if 0 <= i < len(labels) and isinstance(v, str) and v.strip():
            out[labels[i]] = v.strip()
    return out

def _merge_answer_objects(raw):
    """Collect EVERY {"answers": {...}} block in the model's output and merge them.

    Small local models routinely emit two "answers" keys in one object —
    {"answers":{"A":"x"},"answers":{"B":"y"}} — and json.loads keeps only the last, silently
    throwing away perfectly good answers. That is why obvious questions came back unanswered
    while the model had in fact answered them."""
    out = {}
    if not raw:
        return out
    for m in re.finditer(r'"answers"\s*:\s*(\{)', raw):
        i = m.start(1)
        depth, j = 0, i
        while j < len(raw):                      # walk to the matching brace
            if raw[j] == "{":
                depth += 1
            elif raw[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        try:
            for k, v in (json.loads(raw[i:j + 1]) or {}).items():
                if isinstance(v, str) and v.strip() and k not in out:
                    out[k] = v
        except Exception:
            continue
    # Models also emit answers as SIBLINGS of the "answers" object rather than inside it:
    #   {"answers":{...}, "current_location":"Buffalo, NY", "Are you ready to work…":"true"}
    # Reading only the answers block discarded most of a good reply. Take every top-level
    # string too; the caller matches them against the real questions and ignores the rest.
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            obj = json.loads(m.group(0))
            for k, v in (obj or {}).items():
                if k != "answers" and isinstance(v, str) and v.strip() and k not in out:
                    out[k] = v
        except Exception:
            pass
    return out

def _llm_answer_fields(context, fields, extra="", trace=None):
    """Answer NON-sensitive application questions from the candidate's own material, using the
    backend LLM (llm.py: a configured hosted key OR local Ollama). Returns {label: answer}.
    Returns {} when no model is available or on any error — callers then fall back to needs_you.
    The model is instructed to return '' for anything the material doesn't support — never invent.
    `extra` = the operator's editable answer-prompt guidance (tone/perspective), appended to the
    system prompt WITHOUT relaxing the never-invent / JSON-format rules."""
    fields = [f for f in (fields or []) if f.get("label")]
    if not fields or not context:
        return {}
    try:
        import llm
        if not llm.available():
            return {}
        qs = []
        for i, f in enumerate(fields, 1):
            q = {"id": i, "q": f["label"]}
            if f.get("choose_one_of") or f.get("options"):
                q["choose_one_of"] = (f.get("choose_one_of") or f.get("options"))[:14]
            if f.get("candidate_said"):
                q["candidate_said"] = f["candidate_said"]
            qs.append(q)
        sysp = (
            "You are completing a job application on behalf of the candidate, from the material below.\n"
            "Return an entry for EVERY question asked. The value is what should be typed into that "
            "field — no explanations, no apologies, no sentences about what you cannot determine. "
            "If a question genuinely cannot be answered, use an empty string.\n\n"
            "HOW TO DECIDE:\n"
            "1. FACTS about the candidate's history (employers, titles, dates, degrees) come from the "
            "material only. Never invent one. If the material shows no experience in the thing being "
            "asked about, the answer is an empty string — do NOT substitute a related number. "
            "'Years of bookkeeping experience' is not answered by years of software experience.\n"
            "2. DERIVED facts are fine when the material supports them: total years of experience from "
            "a summary or from role dates; whether the candidate is currently working from an unfinished "
            "role; their current or last employer and title.\n"
            "3. WILLINGNESS and PREFERENCE questions — 'are you ready to work EMEA shift timings', "
            "'happy to work N days in the office', 'willing to relocate', 'preferred location', 'when "
            "can you join' — are about intent, not history. Someone applying to a role has accepted its "
            "stated working arrangement, so answer these affirmatively and concretely from the "
            "candidate's stated context. These are NOT facts to look up; leaving them blank is wrong.\n"
            "4. OPEN questions like 'reason for job change' should get a short, professional, "
            "first-person answer grounded in the candidate's situation.\n"
            "5. Never state or imply compensation, immigration status, or demographic information "
            "unless it appears verbatim in the material.\n"
            "6. choose_one_of: reply with EXACTLY one of the listed options. candidate_said is the "
            "candidate's own answer — map it onto the closest listed option rather than discarding it.\n"
            + (("\nExtra guidance from the candidate: " + extra.strip()[:800] + "\n") if extra else "")
            + '\nReturn STRICT JSON and NOTHING else, with one entry per question id, in order:\n'
              '{"answers":[{"id":1,"a":"<value>"},{"id":2,"a":""}]}')
        userp = "CANDIDATE MATERIAL:\n" + context[:6000] + "\n\nQUESTIONS (JSON):\n" + json.dumps(qs)
        raw = llm.gen(sysp, userp, json_mode=True, temp=0, max_tokens=800)
        if trace is not None:
            trace.update({"provider": (llm.available() or ["?"])[0], "system_prompt": sysp,
                          "context": context[:3800], "questions": qs, "raw_response": (raw or "")[:4000]})
        merged = _answers_by_id(raw, [f["label"] for f in fields]) or _merge_answer_objects(raw)
        kept = {k: v.strip() for k, v in merged.items()
                if isinstance(v, str) and not _is_refusal(v)}
        if trace is not None:
            trace["parsed"] = merged
            dropped = [k for k in merged if k not in kept]
            if dropped:
                trace["dropped_as_non_answers"] = dropped
        return kept
    except Exception as e:
        if trace is not None:
            trace["error"] = str(e)[:200]
        return {}

def _is_combobox(el):
    """A React/ARIA typeahead (Ashby location, Greenhouse degree, etc.): looks like a text
    input but needs type→pick-from-listbox, not a plain fill."""
    try:
        return bool(el.evaluate(
            "e=>e.getAttribute('role')==='combobox'||e.getAttribute('aria-haspopup')==='listbox'"
            "||e.getAttribute('aria-autocomplete')==='list'||e.getAttribute('aria-expanded')!==null"))
    except Exception:
        return False

def _fill_combobox(page, el, value):
    """Type into a typeahead and select the best-matching option from its popup listbox.
    Filling the value directly wouldn't register the selection in a React combobox."""
    v = str(value).strip()
    if not v:
        return False
    try:
        el.click(); el.fill(""); el.type(v[:48], delay=25)
        page.wait_for_timeout(750)   # let async options load
        vl = v.lower()
        for sel in ("[role=option]", "li[role=option]", "[role=listbox] li",
                    "[class*=option i]", "[class*=menu i] li", "[class*=result i] li"):
            opts = [o for o in page.query_selector_all(sel) if o.is_visible()]
            if not opts:
                continue
            texts = []
            for o in opts:
                try:
                    texts.append((o.inner_text() or "").strip())
                except Exception:
                    texts.append("")
            mi = _opt_match(v, texts, threshold=0.34)
            pick = opts[mi] if mi is not None else None
            if pick is None:
                # Never settle for "whatever came first". Typing "Buffalo" into a places
                # autocomplete and taking the top suggestion put "Buffalo City, Eastern Cape,
                # South Africa" into a real application. A wrong answer is worse than a blank
                # one — leave it empty so it is reported as needing the candidate.
                first = ""
                try:
                    first = (opts[0].inner_text() or "").strip()
                except Exception:
                    pass
                print(f"  [combo] no suggestion matched {v!r} (top was {first[:40]!r}) — left blank")
                try:
                    el.fill("")
                except Exception:
                    pass
                return False
            pick.click(); page.wait_for_timeout(200)
            return True
        # no popup rendered — fall back to keyboard selection of the first suggestion
        el.press("ArrowDown"); page.wait_for_timeout(150); el.press("Enter")
        return bool(el.evaluate("e=>e.value"))
    except Exception:
        return False

def _fill_questions(page, bank, context=""):
    """Fill selects, radios and text fields from the answer bank; then, for any NON-sensitive
    required question still unanswered, ask the backend LLM from the candidate's material.
    Returns the list of required questions still unanswered (→ needs_you)."""
    bank = bank or {}
    unfilled = []
    pending = []   # (el, meta) non-sensitive required Qs to try via LLM after the deterministic pass
    # selects
    for sel in page.query_selector_all("select"):
        try:
            if not sel.is_visible(): continue
            cur = sel.evaluate("e=>e.value")
            label = _label_text(sel); ans = _answer_for(label, bank)
            if ans and _select_by_text(sel, ans): continue
            if _is_required(sel) and not (cur and cur not in ("", "0")):
                opts = sel.evaluate("e=>[...e.options].map(o=>o.text).filter(t=>t&&!/^select/i.test(t)).slice(0,20)")
                meta = {"label": label or "(dropdown)", "type": "select", "options": opts}
                if _SENSITIVE_RE.search(label or ""):
                    unfilled.append(meta)                      # sensitive → standing only, never LLM
                else:
                    pending.append((sel, meta))
        except Exception:
            continue
    # radio groups
    groups = {}
    for r in page.query_selector_all("input[type='radio']"):
        try:
            nm = r.evaluate("e=>e.name") or ("_" + str(id(r)))
            groups.setdefault(nm, []).append(r)
        except Exception:
            pass
    for nm, rs in groups.items():
        try:
            qlabel = _group_label(rs[0]); ans = _answer_for(qlabel, bank)
            already = any((r.evaluate("e=>e.checked") for r in rs))
            if already: continue
            if ans:
                a = str(ans).strip().lower(); picked = False
                for r in rs:
                    rl = _radio_label(r).lower()
                    if rl and (rl.startswith(a) or a.startswith(rl[:4]) or a in rl):
                        try: r.check()
                        except Exception:
                            try: r.click()
                            except Exception: continue
                        picked = True; break
                if picked: continue
            if _is_required(rs[0]):
                unfilled.append({"label": qlabel or "(choice)", "type": "radio", "options": [_radio_label(r) for r in rs][:8]})
        except Exception:
            continue
    # checkboxes, grouped by question. A lone sentence-checkbox is a certification; 2+ under one
    # question is a CHOICE ("Which office would you work from? SF / Santa Clara / Both").
    _cbg = {}
    for cb in page.query_selector_all("input[type='checkbox']"):
        try:
            if not cb.is_visible() or cb.evaluate("e=>e.checked"): continue
            _cbg.setdefault(_group_label(cb) or _label_text(cb) or "", []).append(cb)
        except Exception:
            continue
    for glabel, cbs in _cbg.items():
        try:
            req = any(_is_required(c) for c in cbs)
            if len(cbs) == 1:                                    # single certification checkbox
                c = cbs[0]
                if not req:
                    continue
                lab = glabel or _label_text(c)
                if _LEGAL_CONSENT.search(lab or ""):
                    unfilled.append({"label": (lab or "(agreement)")[:90], "type": "consent"})
                else:
                    try: c.check()
                    except Exception:
                        try: c.click()
                        except Exception: unfilled.append({"label": lab or "(checkbox)", "type": "checkbox"})
                continue
            if not req:                                          # optional multi-select → leave it
                continue
            if _SENSITIVE_RE.search(glabel or ""):
                unfilled.append({"label": glabel or "(choose)", "type": "checkgroup",
                                 "options": [_radio_label(c) for c in cbs][:8]})
                continue
            opts = [(_radio_label(c) or "", c) for c in cbs]
            pick = None
            for txt, c in opts:                                  # 1) prefer an all-encompassing option
                if re.search(r"\b(both|any|all locations?|open to all|no preference|flexible|either)\b", txt.lower()):
                    pick = c; break
            if not pick:                                         # 2) an option matching a saved answer
                a = _answer_for(glabel, bank)
                if a:
                    mi = _opt_match(a, [txt for txt, _ in opts])
                    pick = opts[mi][1] if mi is not None else None
            if pick:
                try: pick.check()
                except Exception:
                    try: pick.click()
                    except Exception: pass
            else:                                                # 3) let the LLM choose an option
                pending.append((cbs, {"label": glabel or "(choose)", "type": "checkgroup",
                                      "options": [t for t, _ in opts]}))
        except Exception:
            continue
    # Yes/No (and segmented) questions rendered as BUTTONS, not radios (Ashby-style)
    _yn = {}
    for b in page.query_selector_all("button, [role=button]"):
        try:
            if not b.is_visible():
                continue
            txt = (b.inner_text() or "").strip().lower()
            if txt not in ("yes", "no"):
                continue
            q = _group_label(b)
            if not q:
                continue
            if q not in _yn:
                a = _answer_for(q, bank)
                if a is None:
                    a = _default_optout(q)                       # decline marketing opt-ins by default
                _yn[q] = (str(a).strip().lower() if a is not None else None)
                if a is None:                                    # sensitive → standing only; else ask
                    unfilled.append({"label": q, "type": "yesno"})
            want = _yn[q]
            want = "yes" if want in ("yes", "y", "true", "1") else "no" if want in ("no", "n", "false", "0") else None
            if want and txt == want:
                try: b.click()
                except Exception: pass
        except Exception:
            continue
    # remaining text / textarea / url / number / date
    for el in page.query_selector_all("input[type='text'],input:not([type]),textarea,input[type='url'],input[type='tel'],input[type='number'],input[type='date']"):
        try:
            if not el.is_visible(): continue
            if el.evaluate("e=>e.value"): continue
            label = _label_text(el); ans = _answer_for(label, bank)
            if _looks_date(label, el):
                dobj = _date_obj(ans)               # None unless we actually have an answer
                if dobj and _fill_date(el, dobj): continue
                if _is_required(el):
                    unfilled.append({"label": label or "(date)", "type": "date"})
                continue
            if _is_combobox(el):
                if ans and _fill_combobox(page, el, ans): continue
                d = _default_optout(label)                     # decline marketing opt-ins by default
                if d and _fill_combobox(page, el, d): continue
                if _is_required(el):
                    meta = {"label": label or "(choice)", "type": "combo"}
                    if _SENSITIVE_RE.search(label or ""):
                        unfilled.append(meta)                  # sensitive → standing only
                    else:
                        pending.append((el, meta))
                continue
            if ans:
                el.fill(str(ans)); continue
            if _is_required(el):
                hint = (el.evaluate("e=>e.placeholder||e.name||e.id||''") or "").replace("_", " ").strip()
                meta = {"label": label or hint or "(text field)", "type": "text", "name": hint}
                if _SENSITIVE_RE.search(label or ""):
                    unfilled.append(meta)                      # sensitive → standing only, never LLM
                else:
                    pending.append((el, meta))
        except Exception:
            continue
    # LLM pass: answer the non-sensitive required questions from the candidate's own material.
    answered = _llm_answer_fields(context, [m for _, m in pending], (bank or {}).get("_answer_prompt", "")) if (pending and context) else {}
    def _lookup(label):
        """Exact label, else best fingerprint overlap — a small local model often reworards the
        JSON key it echoes back, so an exact-key lookup alone drops otherwise-good answers."""
        if label in answered:
            return answered[label]
        lf = _fp(label); best, bs = None, 0
        for k, v in answered.items():
            kf = _fp(k); sh = len(lf & kf)
            if sh > bs and sh >= 2 and sh >= 0.6 * min(len(lf), len(kf)):
                bs, best = sh, v
        return best
    for el, meta in pending:
        a = _lookup(meta["label"])
        done = False
        if a:
            try:
                if meta["type"] == "select":
                    done = _select_by_text(el, a)
                elif meta["type"] == "combo":
                    done = _fill_combobox(page, el, a)
                elif meta["type"] == "checkgroup":              # el is the list of checkboxes
                    labels = [_radio_label(c) or "" for c in el]
                    mi = _opt_match(a, labels)
                    if mi is not None:
                        try: el[mi].check(); done = True
                        except Exception:
                            try: el[mi].click(); done = True
                            except Exception: pass
                else:
                    el.fill(a); done = True
            except Exception:
                done = False
        if not done:
            unfilled.append(meta)                              # model couldn't answer → ask the user
    # de-dup by label
    seen=set(); out=[]
    for u in unfilled:
        k=(u["label"],u["type"])
        if k not in seen: seen.add(k); out.append(u)
    return out

# legal AGREEMENTS the applicant must accept themselves (never auto-checked)
_LEGAL_CONSENT = re.compile(
    r"arbitrat|terms (of|and)|conditions|privacy policy|legally bound|waive|binding|"
    r"consent to (the )?(processing|terms|agreement)|agree to (the )?(terms|arbitration|agreement)|"
    r"e-?sign|electronic signature", re.I)
_SUCCESS = ["thank you", "application submitted", "received your application", "we received",
            "your application has been", "successfully", "application complete"]

def _find_submit(frame, page, pack):
    for sel in pack["submit"] + ["button:has-text('Submit application')", "button:has-text('Submit')",
                                 "button:has-text('Send application')", "button:has-text('Finish')",
                                 "input[type='submit']", "[role=button]:has-text('Submit')"]:
        for scope in (frame, page):
            try:
                b = scope.query_selector(sel)
                if b and b.is_visible():
                    return b
            except Exception:
                continue
    return None

def _errors(frame):
    try:
        return frame.evaluate("""()=>[...document.querySelectorAll('[aria-invalid=\"true\"],[class*=error i],[class*=invalid i],[role=alert]')]
            .filter(e=>e.offsetParent&&(e.innerText||'').trim()).slice(0,6).map(e=>(e.innerText||'').trim().slice(0,90))""") or []
    except Exception:
        return []

def _verify(page, frame):
    """After a submit click: 'confirmed' (success text), 'sent' (form gone, no proof),
    or 'stuck' (still on the form — the board bounced it for corrections)."""
    try:
        body = (page.inner_text("body") + " " + (frame.inner_text("body") if frame is not page else "")).lower()
    except Exception:
        body = ""
    if any(t in body for t in _SUCCESS):
        return "confirmed"
    try:
        still = bool(frame.query_selector(_EMAIL_SEL))
    except Exception:
        try: still = bool(page.query_selector(_EMAIL_SEL))
        except Exception: still = False
    return "stuck" if still else "sent"

def _fix_invalid(frame):
    """After a validation bounce, tick benign required checkboxes the board flagged (leaving
    legal agreements alone). Returns how many were fixed."""
    n = 0
    for cb in frame.query_selector_all("input[type='checkbox']"):
        try:
            if not cb.is_visible() or cb.evaluate("e=>e.checked"):
                continue
            flagged = _is_required(cb) or cb.evaluate("e=>e.getAttribute('aria-invalid')==='true'")
            if not flagged:
                continue
            label = _label_text(cb) or _radio_label(cb)
            if _LEGAL_CONSENT.search(label or ""):
                continue
            try: cb.check(); n += 1
            except Exception:
                try: cb.click(); n += 1
                except Exception: pass
        except Exception:
            continue
    return n

def _file_ctx(el):
    try:
        return (el.evaluate("e=>((e.name||'')+' '+(e.id||'')+' '+((e.closest('[class*=field i],[class*=question i],label,li,div')||{}).innerText||'')).slice(0,140)") or "").lower()
    except Exception:
        return ""

def submit(job, answers, resume_html, standing=None, dry=True, headless=True, timeout=45000, cover_letter=""):
    """Fill (and, if not dry, submit) the apply form. Returns a result dict."""
    url = job.get("url") or job.get("applyUrl")
    if not url:
        return {"ok": False, "status": "error", "detail": "no apply url"}
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(RESUME_DIR, exist_ok=True)
    # a stable path, not a temp file: the exact PDF sent with this application is kept so it
    # can be re-downloaded and re-used, rather than vanishing with the process.
    resume_pdf = os.path.join(RESUME_DIR, "%s-%s.pdf" % (
        re.sub(r"[^a-z0-9]+", "-", str(job.get("company_slug") or job.get("vendor") or "job").lower())[:30],
        re.sub(r"[^a-z0-9]+", "-", str(job.get("id") or job.get("title") or "app").lower())[:40]))
    # submission is driven by the caller (UI "Fill & submit" toggle). APPLY_DRY_ONLY=1 hard-forces dry.
    live = (not dry) and os.environ.get("APPLY_DRY_ONLY") != "1"
    # APPLY_HEADED=1 → run a VISIBLE Chrome window (watch it, or solve a captcha yourself).
    if os.environ.get("APPLY_HEADED") == "1":
        headless = False
    sync_playwright = _pw()
    with sync_playwright() as p:
        # Real Chrome with a PERSISTENT profile (warm cookies/session) → far fewer bot
        # challenges than a cold, fresh context (this is genuine — no spoofing). Falls back
        # to a fresh context if the profile is locked (e.g. another apply already using it).
        browser = ctx = None
        if os.environ.get("APPLY_PERSIST", "1") != "0":
            try:
                ctx = p.chromium.launch_persistent_context(_PROFILE_DIR, headless=headless,
                        channel="chrome", accept_downloads=False)
            except Exception:
                ctx = None
        if ctx is None:
            try:
                browser = p.chromium.launch(headless=headless, channel="chrome")
            except Exception:
                browser = p.chromium.launch(headless=headless)
            ctx = browser.new_context(accept_downloads=False)
        page = ctx.pages[0] if (browser is None and ctx.pages) else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(1500)
            # Only a BLOCKING wall stops us here. Nearly every ATS form ships an invisible
            # reCAPTCHA; aborting on its presence meant never even trying to reach the form.
            if _captcha_blocking(page):
                return {"ok": False, "status": "captcha",
                        "detail": "This page is behind a bot-check (CAPTCHA) — open it yourself to apply."}
            # ensure the application form is on screen: many pages are a description with an
            # "Apply" button, React forms render async, and iCIMS/embeds load the form in an
            # iframe. Reveal it (searching page + frames), then wait for a field anywhere.
            # Consent banners are dismissed BEFORE we look for the form — they overlay the page
            # and swallow the click on "Apply", leaving the form closed (and us reporting a
            # suspiciously empty form as if it were complete).
            _dismiss_consent(page)
            # navigate to the real form — this may hop pages or open a new tab, so adopt
            # whatever page it lands on rather than staying on the description page.
            try:
                import board_agents
                page = board_agents.navigate(page, _vendor_of(url))
            except Exception:
                page = _reveal_apply(page)
            try:
                page.wait_for_selector(_EMAIL_SEL, timeout=9000)
            except Exception:
                pass
            _dismiss_consent(page)         # again: some banners only appear after interaction
            if _captcha_blocking(page):    # the wall is usually on the APPLICATION page, not the description
                return {"ok": False, "status": "captcha", "screenshot": "",
                        "detail": "The application page is behind a bot-check (CAPTCHA) — "
                                  "open it yourself and complete the form."}
            # the form may live inside an embedded ATS iframe (iCIMS, corporate embeds) — from
            # here on, operate on that frame (Playwright Frame shares the query/fill API).
            frame = _form_frame(page)
            pack = _pack(frame.url if frame is not page else url)
            pack_vendor = _vendor_of(frame.url if frame is not page else url)
            # standard fields — Lever/Ashby may use one full-name field instead of first/last
            full = (str(answers.get("first_name", "")) + " " + str(answers.get("last_name", ""))).strip()
            filled = {
                "full_name":  _fill_first(frame, pack["full_name"], full),
                "first_name": _fill_first(frame, pack["first_name"], answers.get("first_name")),
                "last_name":  _fill_first(frame, pack["last_name"], answers.get("last_name")),
                "email":      _fill_first(frame, pack["email"], _clean_contact("email", answers.get("email"))),
                "phone":      _fill_first(frame, pack["phone"], _clean_contact("phone", answers.get("phone"))),
            }
            # résumé upload (render a real PDF, attach to the vendor's file input)
            attached = False
            saved_pdf = ""
            # categorize file inputs: résumé goes to a non-cover input; a cover-labeled input
            # (if any, and if we have a cover letter) gets the cover-letter PDF — never crossed.
            file_inputs = []
            for sel in pack["file"]:
                for f in frame.query_selector_all(sel):
                    if f not in file_inputs:
                        file_inputs.append(f)
            cover_input = next((f for f in file_inputs if "cover" in _file_ctx(f)), None)
            resume_input = next((f for f in file_inputs if f is not cover_input), None)
            # The résumé is built in PARALLEL with navigation — tailoring takes ~30s and the
            # browser needs ~10-20s to reach the form, so waiting for it up front wasted that
            # whole window. Resolve it here, at the one moment we actually need the bytes.
            resume_html = _resolve_resume(resume_html)
            # Render and KEEP the PDF whether or not there's a field to attach it to. A board
            # with no upload field (or one the agent can't reach) is exactly when the candidate
            # needs the tailored résumé in hand to finish by hand — throwing it away there was
            # backwards.
            if resume_html:
                try:
                    _render_pdf(ctx, resume_html, resume_pdf)
                    saved_pdf = resume_pdf
                except Exception:
                    saved_pdf = ""
            if resume_input and saved_pdf:
                try:
                    resume_input.set_input_files(saved_pdf); attached = True
                except Exception:
                    attached = False
            # cover letter: upload to a dedicated file input, else fill a "cover letter" textarea
            if cover_letter:
                try:
                    if cover_input:
                        cpath = os.path.join(tempfile.gettempdir(), f"cover_{int(time.time())}.pdf")
                        _cl = cover_letter.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
                        _render_pdf(ctx, "<html><body style='font-family:Georgia,serif;padding:48px;line-height:1.55;font-size:12pt'>"
                                    + _cl + "</body></html>", cpath)
                        cover_input.set_input_files(cpath)
                    else:
                        for el in frame.query_selector_all("textarea, input[type='text']"):
                            if not el.is_visible() or el.evaluate("e=>e.value"): continue
                            lab = (_label_text(el) + " " + (el.evaluate("e=>(e.name||'')+' '+(e.placeholder||'')") or "")).lower()
                            if "cover" in lab and ("letter" in lab or el.evaluate("e=>e.tagName") == "TEXTAREA"):
                                el.fill(cover_letter[:5000]); break
                except Exception:
                    pass
            # best-effort: fill remaining visible required text inputs from answers-by-name
            for name, val in (answers or {}).items():
                if name in ("first_name", "last_name", "email", "phone") or val in (None, ""):
                    continue
                for sel in (f"[name='{name}']", f"#{name}"):
                    try:
                        el = frame.query_selector(sel)
                        if el and el.is_visible():
                            (el.select_option(label=str(val)) if el.evaluate("e=>e.tagName")=="SELECT" else el.fill(str(val)))
                            break
                    except Exception:
                        continue
            _bank = dict(standing or {})
            for _k in ("first_name", "last_name", "email", "phone"):
                if answers.get(_k):
                    _bank.setdefault(_k, answers[_k])
            if full:
                _bank.setdefault("full_name", full)
            # material the LLM may answer NON-sensitive free-text from — the candidate's own
            # résumé + role + explicitly-provided facts (never anything invented). A persona
            # (standing._persona or APPLY_PERSONA) frames ambiguous non-sensitive answers —
            # e.g. "international student on F-1/OPT". Sensitive Qs still come ONLY from standing.
            _facts = {k: v for k, v in (standing or {}).items() if k not in ("_custom", "_persona") and v}
            _persona = (standing or {}).get("_persona") or os.environ.get("APPLY_PERSONA", "")
            # Facts and persona come FIRST and are never truncated. Putting the résumé first
            # let it consume the whole budget — the persona was being cut off mid-word ("…: I"),
            # so the model answered with no idea who it was speaking for.
            context = ("Role: " + (job.get("title") or "")
                       + ("\nCandidate context (assume this where a non-sensitive question is ambiguous): "
                          + _persona if _persona else "")
                       + ("\nKnown facts (use these directly): " + json.dumps(_facts) if _facts else "")
                       + "\nCandidate résumé:\n" + _strip_html(resume_html)[:3000])
            # PLAN-THEN-FILL: read every field into a schema, answer the whole form in one
            # pass (real facts first, then a single LLM call that sees all the questions
            # together), then apply it. Falls back to the incremental filler on any error so
            # a planner problem can never make the agent worse than before.
            plan_out = None
            if os.environ.get("APPLY_PLANNER", "1") != "0":
                try:
                    import board_agents
                    plan_out = board_agents.run(page, frame, pack_vendor, _bank, context,
                                                (_bank or {}).get("_answer_prompt", ""))
                    unfilled_required = plan_out["unfilled_required"]
                except Exception as e:
                    print("  [planner] falling back:", str(e)[:120])
                    plan_out = None
            if plan_out is None:
                unfilled_required = _fill_questions(frame, _bank, context)
            shot = os.path.join(OUT_DIR, f"{re.sub(r'[^a-z0-9]+','-',(job.get('title') or 'job').lower())[:40]}-{int(time.time())}.png")
            page.screenshot(path=shot, full_page=True)
            # DID THE FORM ACTUALLY OPEN? A page still showing the job description (consent wall,
            # an "Apply" click that didn't land, a form behind a login) has no résumé input and
            # almost no fields — and would otherwise be reported as "nothing left to fill",
            # which reads as success. Say plainly that we never reached a real form instead.
            n_std = sum(1 for v in filled.values() if v)
            warnings = []
            if not attached:
                warnings.append("the résumé could not be attached (no upload field found)")
            # A page still showing only the job description — consent wall, an "Apply" click that
            # didn't land, a form behind a login — has no résumé input AND almost no fields. It
            # would otherwise report "nothing left to fill", which reads as success.
            form_ready = bool(file_inputs) or n_std >= 2
            if not form_ready:
                warnings.append("almost none of the standard fields (name/email/phone) were present")
            prepared = {"filled": filled, "resume_attached": attached, "screenshot": shot,
                        "resume_pdf": saved_pdf,      # kept even when it couldn't be attached
                        "unfilled_required": unfilled_required, "warnings": warnings,
                        "form_ready": form_ready}
            if plan_out:                       # what the agent read, decided, and completed
                prepared["field_plan"] = plan_out["plan"]
                prepared["field_schema"] = plan_out["schema"]
                prepared["filled_labels"] = plan_out["filled_labels"]
                prepared["plan_counts"] = plan_out["counts"]
                prepared["field_trace"] = plan_out.get("trace") or {}
            if not form_ready:
                return {"ok": False, "status": "form_not_ready",
                        "detail": "Never reached a real application form — " + "; ".join(warnings)
                                  + ". Open the link yourself to check.", **prepared}
            if not attached:
                # the form is genuinely open, but an application without a résumé is not an
                # application — say so precisely instead of implying the whole page failed.
                return {"ok": False, "status": "needs_answers",
                        "detail": "Form reached and filled, but no résumé upload field was found — "
                                  "attach it yourself, or use the employer's direct apply link.",
                        **prepared}
            if not live:
                return {"ok": True, "status": "dry_prepared", "detail": "Form prepared (not submitted).", **prepared}
            # An application without the résumé is not an application — never send one.
            if not attached:
                return {"ok": False, "status": "needs_answers",
                        "detail": "Not submitted — the résumé could not be attached.", **prepared}
            # HONEST GATE: never click submit while REQUIRED questions are unanswered.
            # Submitting a half-filled form (then reporting "sent") is the faking we refuse
            # to do — surface exactly what's missing so it can be answered, then finish.
            if unfilled_required:
                return {"ok": False, "status": "needs_answers",
                        "detail": "Not submitted — " + str(len(unfilled_required)) +
                                  " required question(s) still need answers. Fill them and it'll submit.",
                        **prepared}
            # LIVE submit
            btn = _find_submit(frame, page, pack)
            if not btn:
                return {"ok": False, "status": "no_submit_button", "detail": "Couldn't find the submit button — apply manually.", **prepared}
            url_before = page.url
            btn.click(); page.wait_for_timeout(4000)
            if _captcha_blocking(page):
                return {"ok": False, "status": "captcha", "detail": "CAPTCHA appeared on submit — manual.", **prepared}
            page.screenshot(path=shot, full_page=True)
            v = _verify(page, frame)
            if v == "confirmed":
                return {"ok": True, "status": "submitted", "sent": True, "confirmed": True,
                        "confirm_url": page.url, "detail": "Submitted — confirmation page detected.", **prepared}
            if v == "sent":
                return {"ok": False, "status": "unconfirmed", "sent": True, "confirmed": False,
                        "confirm_url": page.url,
                        "detail": "Submitted, but no confirmation page detected — verify manually.", **prepared}
            # v == "stuck": the board bounced us for corrections. VALIDATION step — try to fix the
            # flagged fields (tick benign required checkboxes, re-fill anything now empty/revealed)
            # and resubmit ONCE, the way a person handles "you missed a field".
            if page.url == url_before:
                _fix_invalid(frame)
                refill = _fill_questions(frame, _bank, context)
                if not refill:
                    btn2 = _find_submit(frame, page, pack)
                    if btn2:
                        btn2.click(); page.wait_for_timeout(4000)
                        page.screenshot(path=shot, full_page=True)
                        v2 = _verify(page, frame)
                        if v2 == "confirmed":
                            return {"ok": True, "status": "submitted", "sent": True, "confirmed": True,
                                    "confirm_url": page.url, "detail": "Submitted — confirmation page detected (after fixing a flagged field).", **prepared}
                        if v2 == "sent":
                            return {"ok": False, "status": "unconfirmed", "sent": True, "confirmed": False,
                                    "confirm_url": page.url,
                                    "detail": "Submitted, but no confirmation page detected — verify manually.", **prepared}
                errors = _errors(frame)
                rescan = refill or _fill_questions(frame, _bank) or unfilled_required
                detail = "The form didn't go through — it still needs answers"
                if errors:
                    detail += ": " + "; ".join(dict.fromkeys(errors))[:180]
                return {"ok": False, "status": "needs_answers", "detail": detail,
                        **{**prepared, "unfilled_required": rescan}}
            # URL changed but no confirmation text — probably sent, unproven.
            return {"ok": False, "status": "unconfirmed", "sent": True, "confirmed": False,
                    "confirm_url": page.url,
                    "detail": "Submitted, but no confirmation page detected — verify manually.", **prepared}
        except Exception as e:
            return {"ok": False, "status": "browser_error", "detail": str(e)[:200]}
        finally:
            try: ctx.close()
            except Exception: pass
            if browser is not None:
                try: browser.close()
                except Exception: pass

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--headed", action="store_true", help="show the browser")
    args = ap.parse_args()
    demo = {"url": args.url, "title": "demo"}
    ans = {"first_name": "Alex", "last_name": "Morgan", "email": "alex@example.com", "phone": "+1 555 0100"}
    resume = "<html><body style='font-family:sans-serif'><h1>Alex Morgan</h1><p>Frontend Engineer</p></body></html>"
    standing = {"work_authorized":"Yes","needs_sponsorship":"Yes","citizenship":"India",
        "visa_type":"F-1 OPT","work_auth_basis":"Temporary work authorization",
        "current_location":"Buffalo, NY","current_company":"Blackstone Launchpad",
        "linkedin":"linkedin.com/in/alexmorgan","github":"github.com/alexm","portfolio":"alexmorgan.dev",
        "relocate":"Yes","remote_ok":"Yes","desired_location":"United States","salary_expectation":"$150,000",
        "gender":"Male","ethnicity":"Prefer not to say","veteran":"No","disability":"No","over_18":"Yes"}
    res = submit(demo, ans, resume, standing=standing, dry=True, headless=not args.headed)  # always dry here — never submits
    print(res)
