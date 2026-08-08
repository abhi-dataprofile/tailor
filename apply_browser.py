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

CAPTCHA_HINTS = ["recaptcha", "hcaptcha", "g-recaptcha", "cf-turnstile", "data-sitekey", "are you human"]

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
}

def _vendor_of(url):
    u = (url or "").lower()
    if "lever.co" in u: return "lever"
    if "ashbyhq" in u: return "ashby"
    if "greenhouse" in u: return "greenhouse"
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

def _has_captcha(page):
    # Only a VISIBLE challenge widget counts — many sites load the recaptcha/turnstile
    # script defensively without ever showing a challenge. Checking raw HTML over-blocks.
    sels = ["iframe[src*='recaptcha/api2/anchor']", "iframe[src*='recaptcha/enterprise/anchor']",
            "iframe[src*='hcaptcha.com']", "iframe[src*='challenges.cloudflare.com']",
            "div.g-recaptcha[data-sitekey]", ".h-captcha", "#cf-turnstile"]
    for sel in sels:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            continue
    return False

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
 ("current_location", ["current location","where are you located","currently located","where do you live","based in","city, state","location (city","your location","where are you based"]),
 ("current_company", ["current company","current employer","previous employer","most recent employer"]),
 ("linkedin", ["linkedin"]),
 ("github", ["github"]),
 ("portfolio", ["portfolio"]),
 ("website", ["personal website","other website","personal url","website"]),
 ("work_authorized", ["authorized to work","legally authorized","eligible to work","right to work","authorization to work","work authorization in"]),
 ("needs_sponsorship", ["require sponsorship","need sponsorship","visa sponsorship","sponsorship for employment","require the company to file","now or in the future require","require our company to file","require immigration"]),
 ("citizenship", ["citizenship","country of citizenship","citizen of"]),
 ("visa_type", ["type of visa","which visa","work permit","immigration status","current visa","visa status"]),
 ("work_auth_basis", ["authorization basis","basis for your work","work authorization type","authorization status"]),
 ("desired_location", ["where would you like to work","preferred location","which location","location(s) you anticipate","location you are applying","country or countries you anticipate"]),
 ("relocate", ["relocat"]),
 ("remote_ok", ["come into the office","work remotely","work from a remote","in-person","onsite","hybrid policy"]),
 ("start_date", ["when can you start","start date","available to start","notice period","how much notice"]),
 ("years_experience", ["years of experience","years of relevant","how many years"]),
 ("salary_expectation", ["salary","compensation expectation","desired pay","expected compensation"]),
 ("over_18", ["over 18","at least 18","18 years of age"]),
 ("gender", ["gender"]),
 ("ethnicity", ["ethnicity","identify my ethnicity","hispanic","latino"]),
 ("veteran", ["veteran"]),
 ("disability", ["disab"]),
 ("how_heard", ["how did you hear"]),
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

def _answer_for(label, bank):
    low = (label or "").lower()
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

def _llm_answer_fields(context, fields):
    """Answer NON-sensitive application questions from the candidate's own material, using the
    backend LLM (llm.py: a configured hosted key OR local Ollama). Returns {label: answer}.
    Returns {} when no model is available or on any error — callers then fall back to needs_you.
    The model is instructed to return '' for anything the material doesn't support — never invent."""
    fields = [f for f in (fields or []) if f.get("label")]
    if not fields or not context:
        return {}
    try:
        import llm
        if not llm.available():
            return {}
        qs = []
        for f in fields:
            qs.append({"q": f["label"], "choose_one_of": f["options"][:12]} if f.get("options") else {"q": f["label"]})
        sysp = ("You are completing a job application AS the candidate, using ONLY the candidate "
                "material provided. If the material does not support an answer, return an empty string "
                "for that question — NEVER invent facts, dates, numbers, employers, or credentials. "
                "For questions with choose_one_of, reply with EXACTLY one of those options. Keep "
                "free-text answers concise, first-person and professional. "
                'Return STRICT JSON: {"answers":{"<exact question text>":"<answer>"}}.')
        userp = "CANDIDATE MATERIAL:\n" + context[:3800] + "\n\nQUESTIONS (JSON):\n" + json.dumps(qs)
        raw = llm.gen(sysp, userp, json_mode=True, temp=0, max_tokens=800)
        m = re.search(r"\{[\s\S]*\}", raw or "")
        obj = json.loads(m.group(0)) if m else {}
        return {k: v.strip() for k, v in (obj.get("answers") or {}).items() if isinstance(v, str) and v.strip()}
    except Exception:
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
            pick = None
            for o in opts:
                t = (o.inner_text() or "").strip().lower()
                if t and (vl[:14] in t or t[:14] in vl):
                    pick = o; break
            pick = pick or opts[0]
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
    answered = _llm_answer_fields(context, [m for _, m in pending]) if (pending and context) else {}
    for el, meta in pending:
        a = answered.get(meta["label"])
        done = False
        if a:
            try:
                if meta["type"] == "select":
                    done = _select_by_text(el, a)
                elif meta["type"] == "combo":
                    done = _fill_combobox(page, el, a)
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

def submit(job, answers, resume_html, standing=None, dry=True, headless=True, timeout=45000):
    """Fill (and, if not dry, submit) the apply form. Returns a result dict."""
    url = job.get("url") or job.get("applyUrl")
    if not url:
        return {"ok": False, "status": "error", "detail": "no apply url"}
    os.makedirs(OUT_DIR, exist_ok=True)
    # submission is driven by the caller (UI "Fill & submit" toggle). APPLY_DRY_ONLY=1 hard-forces dry.
    live = (not dry) and os.environ.get("APPLY_DRY_ONLY") != "1"
    sync_playwright = _pw()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=headless, channel="chrome")
        except Exception:
            browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(accept_downloads=False)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(1500)
            if _has_captcha(page):
                return {"ok": False, "status": "captcha", "detail": "CAPTCHA/bot-check present — must be applied to manually."}
            # ensure the application form is on screen: many pages are a description with an
            # "Apply" button, and React forms (Ashby) render async — click + wait for a field.
            if not page.query_selector("input[type='email'], input[name*='email' i], input[autocomplete='email']"):
                for sel in ["a:has-text('Apply for this job')","button:has-text('Apply for this job')",
                            "a:has-text('Apply now')","button:has-text('Apply now')","a:has-text('Apply')","button:has-text('Apply')"]:
                    try:
                        b = page.query_selector(sel)
                        if b and b.is_visible():
                            b.click(); page.wait_for_timeout(2000); break
                    except Exception:
                        continue
            try:
                page.wait_for_selector("input[type='email'], input[name*='email' i], input[autocomplete='email']", timeout=9000)
            except Exception:
                pass
            # dismiss cookie/consent banners that overlay the form
            for csel in ["#onetrust-accept-btn-handler","button:has-text('Accept all')","button:has-text('Accept')",
                         "button:has-text('Dismiss')","button:has-text('Got it')","button:has-text('I agree')","[aria-label='dismiss']"]:
                try:
                    cb = page.query_selector(csel)
                    if cb and cb.is_visible():
                        cb.click(); page.wait_for_timeout(400); break
                except Exception:
                    continue
            pack = _pack(url)
            # standard fields — Lever/Ashby may use one full-name field instead of first/last
            full = (str(answers.get("first_name", "")) + " " + str(answers.get("last_name", ""))).strip()
            filled = {
                "full_name":  _fill_first(page, pack["full_name"], full),
                "first_name": _fill_first(page, pack["first_name"], answers.get("first_name")),
                "last_name":  _fill_first(page, pack["last_name"], answers.get("last_name")),
                "email":      _fill_first(page, pack["email"], answers.get("email")),
                "phone":      _fill_first(page, pack["phone"], answers.get("phone")),
            }
            # résumé upload (render a real PDF, attach to the vendor's file input)
            attached = False
            file_input = None
            for sel in pack["file"]:
                file_input = page.query_selector(sel)
                if file_input:
                    break
            if file_input:
                pdf_path = os.path.join(tempfile.gettempdir(), f"resume_{int(time.time())}.pdf")
                _render_pdf(ctx, resume_html, pdf_path)
                try:
                    file_input.set_input_files(pdf_path); attached = True
                except Exception:
                    attached = False
            # best-effort: fill remaining visible required text inputs from answers-by-name
            for name, val in (answers or {}).items():
                if name in ("first_name", "last_name", "email", "phone") or val in (None, ""):
                    continue
                for sel in (f"[name='{name}']", f"#{name}"):
                    try:
                        el = page.query_selector(sel)
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
            # résumé + role + explicitly-provided facts (never anything invented).
            _facts = {k: v for k, v in (standing or {}).items() if k != "_custom" and v}
            context = ("Role: " + (job.get("title") or "") + "\nCandidate résumé:\n"
                       + _strip_html(resume_html)[:3400]
                       + ("\nKnown facts: " + json.dumps(_facts) if _facts else ""))
            unfilled_required = _fill_questions(page, _bank, context)
            shot = os.path.join(OUT_DIR, f"{re.sub(r'[^a-z0-9]+','-',(job.get('title') or 'job').lower())[:40]}-{int(time.time())}.png")
            page.screenshot(path=shot, full_page=True)
            prepared = {"filled": filled, "resume_attached": attached, "screenshot": shot, "unfilled_required": unfilled_required}
            if not live:
                return {"ok": True, "status": "dry_prepared", "detail": "Form prepared (not submitted).", **prepared}
            # HONEST GATE: never click submit while REQUIRED questions are unanswered.
            # Submitting a half-filled form (then reporting "sent") is the faking we refuse
            # to do — surface exactly what's missing so it can be answered, then finish.
            if unfilled_required:
                return {"ok": False, "status": "needs_answers",
                        "detail": "Not submitted — " + str(len(unfilled_required)) +
                                  " required question(s) still need answers. Fill them and it'll submit.",
                        **prepared}
            # LIVE submit
            btn = None
            for sel in pack["submit"]:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    break
            if not btn:
                return {"ok": False, "status": "no_submit_button", "detail": "Couldn't find the submit button — apply manually.", **prepared}
            url_before = page.url
            btn.click()
            page.wait_for_timeout(4000)
            if _has_captcha(page):
                return {"ok": False, "status": "captcha", "detail": "CAPTCHA appeared on submit — manual.", **prepared}
            body = ""
            try:
                body = page.inner_text("body").lower()
            except Exception:
                pass
            page.screenshot(path=shot, full_page=True)
            ok = any(t in body for t in ["thank you", "application submitted", "received your application",
                                         "we received", "your application has been", "successfully"])
            if ok:
                return {"ok": True, "status": "submitted", "sent": True, "confirmed": True,
                        "confirm_url": page.url, "detail": "Submitted — confirmation page detected.", **prepared}
            # No success text. Did the board actually accept it, or bounce us back to the
            # form for validation? If the apply form is STILL here (with errors / same URL),
            # it was NOT submitted — say so honestly instead of claiming "sent".
            still_on_form = bool(page.query_selector("input[type='email'], input[name*='email' i], input[autocomplete='email']"))
            errors = []
            try:
                errors = page.evaluate("""()=>[...document.querySelectorAll('[aria-invalid=\"true\"],[class*=error i],[class*=invalid i],[role=alert]')]
                    .filter(e=>e.offsetParent&&(e.innerText||'').trim()).slice(0,5).map(e=>(e.innerText||'').trim().slice(0,90))""") or []
            except Exception:
                pass
            if still_on_form and page.url == url_before:
                rescan = _fill_questions(page, _bank) or unfilled_required
                detail = "The form didn't go through — it still needs answers"
                if errors:
                    detail += ": " + "; ".join(dict.fromkeys(errors))[:180]
                return {"ok": False, "status": "needs_answers", "detail": detail,
                        **{**prepared, "unfilled_required": rescan}}
            # Form is gone and URL changed, but no explicit confirmation text: it was sent,
            # we just couldn't prove receipt. Honest "unconfirmed" (never claimed as applied).
            return {"ok": False, "status": "unconfirmed", "sent": True, "confirmed": False,
                    "confirm_url": page.url,
                    "detail": "Submitted, but no confirmation page detected — verify manually.", **prepared}
        except Exception as e:
            return {"ok": False, "status": "browser_error", "detail": str(e)[:200]}
        finally:
            ctx.close(); browser.close()

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
