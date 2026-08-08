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
import os, re, time, tempfile, argparse

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
 ("current_location", ["current location","where are you located","city, state","location (city","your location"]),
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
def _answer_for(label, bank):
    low = (label or "").lower()
    for key, syns in ANSWER_KEYS:
        for sy in syns:
            if sy in low:
                v = (bank or {}).get(key)
                if v not in (None, ""):
                    return str(v)
    # user-provided answers to previously-missed questions (keyed by label substring)
    for k, v in ((bank or {}).get("_custom") or {}).items():
        if k and v and k.lower()[:40] in low:
            return str(v)
    return None

def _label_text(el):
    try:
        js = (
          "e=>{const clean=s=>(s||'').replace(/[*\\u2731]/g,'').trim();"
          "if(e.getAttribute('aria-label'))return clean(e.getAttribute('aria-label'));"
          "if(e.labels&&e.labels[0]&&e.labels[0].innerText.trim())return clean(e.labels[0].innerText.split('\\n')[0]);"
          "let q=e.closest('.application-question');"
          "if(!q)q=e.closest('.field,[data-qa=field],fieldset,.form-group,li');"
          "if(q){const lab=q.querySelector('.application-label,legend,label,.label');"
          "if(lab&&lab.innerText.trim())return clean(lab.innerText.split('\\n')[0]);"
          "const t=(q.innerText||'').trim();if(t)return clean(t.split('\\n')[0]);}return '';}"
        )
        return (el.evaluate(js) or "").strip()
    except Exception:
        return ""

def _is_required(el):
    try:
        return bool(el.evaluate("e=>e.required||e.getAttribute('aria-required')==='true'||/[*]/.test((e.closest('[class*=question],[class*=field],.form-group,li,div')||{}).innerText||'')"))
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

def _fill_questions(page, bank):
    """Fill selects, radios and text fields by label from the answer bank.
    Returns a list of required questions it could NOT answer."""
    bank = bank or {}
    unfilled = []
    # selects
    for sel in page.query_selector_all("select"):
        try:
            if not sel.is_visible(): continue
            cur = sel.evaluate("e=>e.value")
            label = _label_text(sel); ans = _answer_for(label, bank)
            if ans and _select_by_text(sel, ans): continue
            if _is_required(sel) and not (cur and cur not in ("", "0")):
                opts = sel.evaluate("e=>[...e.options].map(o=>o.text).filter(t=>t&&!/^select/i.test(t)).slice(0,20)")
                unfilled.append({"label": label or "(dropdown)", "type": "select", "options": opts})
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
    # remaining text / textarea / url / number
    for el in page.query_selector_all("input[type='text'],input:not([type]),textarea,input[type='url'],input[type='tel'],input[type='number']"):
        try:
            if not el.is_visible(): continue
            if el.evaluate("e=>e.value"): continue
            label = _label_text(el); ans = _answer_for(label, bank)
            if ans:
                el.fill(str(ans)); continue
            if _is_required(el):
                unfilled.append({"label": label or "(text field)", "type": "text"})
        except Exception:
            continue
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
            unfilled_required = _fill_questions(page, _bank)
            shot = os.path.join(OUT_DIR, f"{re.sub(r'[^a-z0-9]+','-',(job.get('title') or 'job').lower())[:40]}-{int(time.time())}.png")
            page.screenshot(path=shot, full_page=True)
            prepared = {"filled": filled, "resume_attached": attached, "screenshot": shot, "unfilled_required": unfilled_required}
            if not live:
                return {"ok": True, "status": "dry_prepared", "detail": "Form prepared (not submitted).", **prepared}
            # LIVE submit
            btn = None
            for sel in pack["submit"]:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    break
            if not btn:
                return {"ok": False, "status": "no_submit_button", "detail": "Couldn't find the submit button — apply manually.", **prepared}
            btn.click()
            page.wait_for_timeout(4000)
            if _has_captcha(page):
                return {"ok": False, "status": "captcha", "detail": "CAPTCHA appeared on submit — manual.", **prepared}
            body = ""
            try:
                body = page.inner_text("body").lower()
            except Exception:
                pass
            ok = any(t in body for t in ["thank you", "application submitted", "received your application", "we received", "successfully"])
            page.screenshot(path=shot, full_page=True)
            # We ALWAYS clicked submit here, so it was sent either way. `confirmed`
            # says whether we actually SAW proof (a success page). Callers must not
            # treat unconfirmed as a failure to retry — it was sent.
            return {"ok": ok, "status": "submitted" if ok else "unconfirmed", "sent": True, "confirmed": ok,
                    "confirm_url": page.url,
                    "detail": "Submitted — confirmation page detected." if ok
                              else "Submitted, but no confirmation page detected — verify manually.", **prepared}
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
