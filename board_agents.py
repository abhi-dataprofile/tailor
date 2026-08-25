"""board_agents.py — one agent per ATS: navigate → extract → plan → fill.

The original engine walked the DOM and filled fields as it met them, which meant the model
answered each question blind to the rest of the form. These agents work in four explicit
stages instead:

  1. navigate(page)  — get from wherever the job link lands to the real application form.
                       Each board hides it differently (an "I'm interested" button, a
                       separate /apply URL, an iframe, a second hop).
  2. extract(frame)  — read EVERY field into a JSON schema: label, type, required, options.
                       Nothing is filled during this pass.
  3. plan(schema, …) — answer the WHOLE schema at once. Real facts from the candidate's
                       profile first; then a single LLM call that sees every remaining
                       question together, with the résumé and job as context. Sensitive
                       questions never reach the model.
  4. fill(frame, …)  — apply the plan, reporting exactly what landed and what didn't.

The plan is plain JSON with a provenance for every answer, so what the agent intends to
submit can be inspected — and corrected — before anything is sent.
"""
import json, os, re

import memory as ab_memory
import apply_browser as ab          # helpers live there; ab imports us lazily to avoid a cycle


# ─────────────────────────────────────────────────────────────── navigation

def _goto_apply_url(page, already):
    """Boards that host the form at a predictable sibling URL (Lever: /<id>/apply)."""
    url = (page.url or "").split("?")[0]
    if re.search(already, url):
        return page
    try:
        page.goto(url.rstrip("/") + "/apply", wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(1200)
    except Exception:
        pass
    return page


def _click_through(page):
    """Click whatever the apply control is called, across hops and new tabs."""
    return ab._reveal_apply(page)


def _wait_react(page, ms=2500):
    """React forms (Ashby, SmartRecruiters' oneclick-ui) mount after the document loads."""
    try:
        page.wait_for_selector(ab._EMAIL_SEL, timeout=ms)
    except Exception:
        page.wait_for_timeout(900)
    return page


# ── the agents ────────────────────────────────────────────────────────────────
# One per ATS. `match` picks the agent from the URL; `navigate` knows how that board hides
# its form; `notes` records what makes it awkward, so the reason for each step is visible.
AGENTS = [
    {
        "name": "greenhouse",
        "match": lambda u: "greenhouse.io" in u,
        "navigate": lambda p: _click_through(p),
        "notes": "Form is on the board page itself; company career pages embed it, so we "
                 "canonicalise to job-boards.greenhouse.io upstream. Demographics are <select>s.",
    },
    {
        "name": "lever",
        "match": lambda u: "lever.co" in u,
        "navigate": lambda p: _goto_apply_url(p, r"/apply/?$"),
        "notes": "Description at /<id>, form at /<id>/apply. ONE full-name field, not first/last.",
    },
    {
        "name": "ashby",
        "match": lambda u: "ashbyhq.com" in u,
        "navigate": lambda p: _wait_react(_click_through(p)),
        "notes": "React form, mounts late. Yes/No are BUTTONS not radios; location and school "
                 "are typeahead comboboxes that must be picked from the listbox.",
    },
    {
        "name": "smartrecruiters",
        "match": lambda u: "smartrecruiters.com" in u,
        "navigate": lambda p: _wait_react(_click_through(p), 4000),
        "notes": "Apply control reads \"I'm interested\" and leads to /oneclick-ui. Many "
                 "tenants sit behind a DataDome bot-check we will not bypass.",
    },
    {
        "name": "recruitee",
        "match": lambda u: "recruitee.com" in u or "/o/" in u,
        "navigate": lambda p: _wait_react(_click_through(p)),
        "notes": "Self-hosted careers domains embed the form; some tenants have no résumé "
                 "upload field at all, which we report rather than silently skip.",
    },
    {
        "name": "icims",
        "match": lambda u: "icims.com" in u,
        "navigate": lambda p: _wait_react(_click_through(p), 4000),
        "notes": "Form lives inside an iframe; _form_frame switches into it. Account-gated "
                 "tenants can't be completed by an agent.",
    },
    {
        "name": "workday",
        "match": lambda u: "myworkdayjobs.com" in u or "workday.com" in u,
        "navigate": lambda p: _click_through(p),
        "supported": False,
        "notes": "Workday requires creating an ACCOUNT with a password before the form opens. "
                 "Creating accounts and entering passwords is a line the agent does not cross, "
                 "so these are reported as manual rather than half-attempted.",
    },
]

_GENERIC_AGENT = {
    "name": "generic",
    "match": lambda u: True,
    "navigate": lambda p: _click_through(p),
    "notes": "Unknown board — click whatever looks like an apply control and read the form.",
}


def route(url):
    """THE ROUTER: pick the agent that knows this board. Falls back to a generic one."""
    u = (url or "").lower()
    for a in AGENTS:
        try:
            if a["match"](u):
                return a
        except Exception:
            continue
    return _GENERIC_AGENT


def supported(url):
    """False for boards an agent genuinely cannot finish (Workday's account wall)."""
    return route(url).get("supported", True)


def navigate(page, vendor=None):
    """Reach the real application form via the routed agent. May return a NEW tab."""
    if ab._has_form(page):
        return page
    agent = route(page.url or "")
    try:
        page = agent["navigate"](page) or page
    except Exception:
        pass
    if not ab._has_form(page) and agent is not _GENERIC_AGENT:
        try:                                            # board route missed → generic attempt
            page = _click_through(page) or page
        except Exception:
            pass
    return page


# ─────────────────────────────────────────────────────────────── extraction

_SKIP_LABEL = re.compile(r"^(search|filter|sort|language|country code)$", re.I)


def extract(frame):
    """Read every fillable field into a schema. Pure inspection — fills nothing.

    Each entry: {key, label, type, required, options, sensitive} plus the live element(s)
    under "_el" (stripped by to_json)."""
    out, seen = [], set()

    def add(el, label, ftype, required, options=None, group=None):
        label = (label or "").strip()
        if not label or _SKIP_LABEL.match(label):
            return
        key = f"{ftype}:{label.lower()[:70]}"
        if key in seen:
            return
        seen.add(key)
        out.append({"key": key, "label": label[:220], "type": ftype,
                    "required": bool(required), "options": options or [],
                    "sensitive": bool(ab._SENSITIVE_RE.search(label)),
                    "_el": group if group is not None else el})

    # selects
    for sel in frame.query_selector_all("select"):
        try:
            if not sel.is_visible():
                continue
            opts = sel.evaluate("e=>[...e.options].map(o=>o.text).filter(t=>t&&!/^select/i.test(t)).slice(0,40)")
            add(sel, ab._label_text(sel), "select", ab._is_required(sel), opts)
        except Exception:
            continue

    # radio groups
    groups = {}
    for r in frame.query_selector_all("input[type='radio']"):
        try:
            groups.setdefault(r.evaluate("e=>e.name") or ("_" + str(id(r))), []).append(r)
        except Exception:
            continue
    for rs in groups.values():
        try:
            add(rs[0], ab._group_label(rs[0]), "radio", ab._is_required(rs[0]),
                [ab._radio_label(r) for r in rs][:40], group=rs)
        except Exception:
            continue

    # checkboxes — one is a consent/certification, several under one question is a choice
    cbg = {}
    for cb in frame.query_selector_all("input[type='checkbox']"):
        try:
            if not cb.is_visible():
                continue
            cbg.setdefault(ab._group_label(cb) or ab._label_text(cb) or "", []).append(cb)
        except Exception:
            continue
    for glabel, cbs in cbg.items():
        try:
            if len(cbs) == 1:
                lab = glabel or ab._label_text(cbs[0])
                kind = "consent" if ab._LEGAL_CONSENT.search(lab or "") else "checkbox"
                add(cbs[0], lab, kind, ab._is_required(cbs[0]))
            else:
                add(cbs[0], glabel, "checkgroup", any(ab._is_required(c) for c in cbs),
                    [ab._radio_label(c) for c in cbs][:40], group=cbs)
        except Exception:
            continue

    # yes/no rendered as buttons (Ashby-style segmented controls)
    yn = {}
    for b in frame.query_selector_all("button, [role=button]"):
        try:
            if not b.is_visible():
                continue
            t = (b.inner_text() or "").strip().lower()
            if t not in ("yes", "no"):
                continue
            q = ab._group_label(b)
            if q:
                yn.setdefault(q, []).append(b)
        except Exception:
            continue
    for q, btns in yn.items():
        add(btns[0], q, "yesno", True, ["Yes", "No"], group=btns)

    # free text / typeaheads / dates
    for el in frame.query_selector_all(
            "input[type='text'],input:not([type]),textarea,input[type='url'],"
            "input[type='tel'],input[type='number'],input[type='date'],input[type='email']"):
        try:
            if not el.is_visible() or el.evaluate("e=>e.value"):
                continue
            label = ab._label_text(el)
            if ab._looks_date(label, el):
                ftype = "date"
            elif ab._is_combobox(el):
                ftype = "combo"
            elif el.evaluate("e=>e.tagName") == "TEXTAREA":
                ftype = "textarea"
            else:
                ftype = "text"
            add(el, label, ftype, ab._is_required(el))
        except Exception:
            continue

    # A React combobox is an <input> with a listbox, so the same control is picked up twice —
    # once as "combo", once as plain "text". Filling the combo then left its twin looking
    # unanswered, which is why fields showed as BOTH filled and still-missing.
    combos = {f["label"].lower() for f in out if f["type"] == "combo"}
    out = [f for f in out if not (f["type"] in ("text", "textarea", "date")
                                  and f["label"].lower() in combos)]
    return out


def to_json(schema):
    """The schema without live element handles — inspectable, loggable, diffable."""
    return [{k: v for k, v in f.items() if k != "_el"} for f in schema]


# ─────────────────────────────────────────────────────────────── planning

def _json_array(raw, key):
    """The array under `key` in a model reply, read by walking brackets rather than a lazy
    regex — a lazy one stops at the first ']' inside a nested value and the whole reply is
    discarded as malformed."""
    if not raw:
        return []
    m = re.search(r'"' + key + r'"\s*:\s*(\[)', raw)
    if not m:
        return []
    i = m.start(1)
    depth, j = 0, i
    while j < len(raw):
        if raw[j] == "[":
            depth += 1
        elif raw[j] == "]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    try:
        return json.loads(raw[i:j + 1]) or []
    except Exception:
        return []


def _semantic_recall(unknown, bank, episodes, trace=None):
    """Map questions the agent has never seen onto answers it already holds.

    Deterministic synonym matching can only ever cover the phrasings someone thought to list.
    This asks the model a different question — not "what is the answer" but "is this the same
    question as one already answered" — which is what actually generalises across boards.
    Nothing new is invented: the model may only point at an existing answer, or decline.
    """
    if not unknown:
        return {}
    # numeric ids on BOTH sides: ids made of question text broke JSON parsing the moment a
    # remembered question contained a quote or a bracket
    known = []
    for k, v in (bank or {}).items():
        if not str(k).startswith("_") and str(v).strip():
            known.append({"means": k.replace("_", " "), "answer": str(v)[:120]})
    for e in ab_memory.answered_episodes(episodes)[:120]:
        known.append({"means": e["q"][:110], "answer": (e.get("a") or "")[:120]})
    for i, k in enumerate(known, 1):
        k["id"] = i
    if not known:
        return {}
    try:
        import llm
        if not llm.available():
            return {}
        sysp = (
            "You match a new job-application question to an answer the candidate has ALREADY "
            "given. You never write a new answer — you only decide which known answer, if any, "
            "answers the same thing.\n\n"
            "MATCH when two questions ask for the same underlying fact, however differently "
            "worded. Boards phrase one thing many ways, and that is exactly what you are here "
            "to see through:\n"
            "  'Kindly state your total professional tenure in years' = 'How many years of "
            "experience do you have overall?'\n"
            "  'By when could you commence employment?' = 'What is your notice period?' = "
            "'Earliest start date'\n"
            "  'In which language would you prefer to communicate?' = 'What is your preferred "
            "language?'\n"
            "  'Where are you presently based?' = 'What is your current place of residence?'\n\n"
            "DO NOT MATCH when the new question is NARROWER than the known one. A question "
            "about a specific technology, domain or employer needs an answer about that same "
            "specific thing:\n"
            "  'How many years of Kubernetes administration?' is NOT answered by overall years "
            "of experience.\n"
            "  'Years of bookkeeping experience' is NOT answered by years of software "
            "experience.\n\n"
            "Reply with the numeric id of the matching known answer, or 0 when nothing "
            "genuinely matches. A wrong match puts a false statement on a real application; an "
            "unnecessary 0 just means the candidate is asked once more.\n"
            "Echo the matched known question in 'known_text' so the match can be checked.\n"
            'Return STRICT JSON and nothing else: '
            '{"matches":[{"id":1,"known":0,"known_text":""}]}')
        userp = ("KNOWN ANSWERS:\n" + json.dumps(known)[:6000]
                 + "\n\nNEW QUESTIONS:\n"
                 + json.dumps([{"id": i + 1, "q": f["label"]} for i, f in enumerate(unknown)]))
        raw = llm.gen(sysp, userp, json_mode=True, temp=0, max_tokens=900)
        if trace is not None:
            trace["recall"] = {"known_count": len(known), "asked": len(unknown),
                               "system_prompt": sysp, "raw_response": (raw or "")[:2500]}
        rows = _json_array(raw, "matches")
    except Exception as e:
        if trace is not None:
            trace["recall"] = {"error": str(e)[:160]}
        return {}
    # Models answer this in whatever shape they like: "known" as a number OR as the question
    # text, sometimes under "known_text", usually with the answer echoed alongside. Resolve by
    # TEXT first — that is what they produce reliably — with the index only as a fallback, and
    # drop anything that cannot be reconciled. An off-by-one once matched "by when could you
    # commence employment?" to "English".
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            i = int(r.get("id", 0)) - 1
        except Exception:
            continue
        if not (0 <= i < len(unknown)):
            continue
        claim = r.get("known")
        text = (r.get("known_text") or (claim if isinstance(claim, str) else "") or "").strip()
        kid = None
        if text:
            tf = ab._fp(text)
            best, score = None, 0.0
            for n, k in enumerate(known):
                kf = ab._fp(k["means"])
                if not kf or not tf:
                    continue
                sc = len(tf & kf) / max(1, min(len(tf), len(kf)))
                if sc > score:
                    score, best = sc, n
            if score >= 0.6:
                kid = best
        if kid is None and isinstance(claim, (int, float)) and not isinstance(claim, bool):
            n = int(claim) - 1
            if 0 <= n < len(known):
                kid = n
        if kid is None:
            # It sometimes echoes the NEW question rather than the remembered one. The answer
            # it echoes is still checkable: accept it only if it is EXACTLY an answer we
            # already hold, so nothing new can enter this way.
            ans0 = (r.get("answer") or "").strip()
            if ans0:
                kid = next((n for n, k in enumerate(known)
                            if k["answer"].strip().lower() == ans0.lower()), None)
        if kid is None:
            if text and trace is not None:
                trace.setdefault("recall_rejected", []).append(
                    {"q": unknown[i]["label"][:80], "claimed": text[:70]})
            continue
        ans = (r.get("answer") or "").strip()      # if it echoed an answer, it must be ours
        if ans and ans[:60].lower() != known[kid]["answer"][:60].lower():
            if trace is not None:
                trace.setdefault("recall_rejected", []).append(
                    {"q": unknown[i]["label"][:80], "claimed": text[:70],
                     "answer_mismatch": ans[:50]})
            continue
        out[unknown[i]["key"]] = known[kid]["answer"]
    return out


def plan(schema, bank, context="", extra_prompt="", trace=None, episodes=None):
    """Answer the whole form at once. Returns {key: {answer, source}}.

    Order of authority, highest first:
      · the candidate's own saved answers (answer bank / standing facts)
      · for SENSITIVE questions, that is the ONLY source — never the model
      · one LLM call for everything still unanswered, seeing all of it together

    Every field's outcome is recorded in `trace["decisions"]` with the reason, so the whole
    form can be read back as: what was asked, what we knew, what we did, and why.
    """
    out, ask, why = {}, [], {}
    episodes = episodes or []
    for f in schema:
        # 1. this exact question, met before on any board
        a = ab_memory.direct_recall(f["label"], episodes)
        if a:
            out[f["key"]] = {"answer": str(a), "source": "memory"}
            why[f["key"]] = "you answered this exact question before — recalled from memory"
            continue
        # 2. a structured fact, matched by meaning
        a = ab._answer_for(f["label"], bank)
        if a not in (None, ""):
            out[f["key"]] = {"answer": str(a), "source": "profile"}
            why[f["key"]] = "you answered this before — reused from your profile"
            continue
        if f["type"] == "consent":
            why[f["key"]] = "a legal agreement — only you can accept it"
            continue
        if f["sensitive"]:
            d = ab._default_optout(f["label"])
            if d:
                out[f["key"]] = {"answer": d, "source": "default"}
                why[f["key"]] = "a marketing opt-in — declined by default"
                continue
            why[f["key"]] = ("legal / compensation / demographic — never answered by the model; "
                             "add it under Add answers and it is reused everywhere")
            continue
        d = ab._default_optout(f["label"])
        if d:
            out[f["key"]] = {"answer": d, "source": "default"}
            why[f["key"]] = "a marketing opt-in — declined by default"
            continue
        if f["required"]:
            ask.append(f)
            why[f["key"]] = "nothing saved for it — asked the model"
        else:
            why[f["key"]] = "optional and nothing saved — left blank"

    # A saved answer that matches NO option is useless to the form: "Yes" against a select
    # whose choices are full sentences fills nothing and the field stays silently blank.
    for f in schema:
        if not f["options"] or f["key"] not in out:
            continue
        chosen = out[f["key"]]["answer"]
        if ab._opt_match(chosen, f["options"]) is not None:
            continue
        if f["sensitive"]:
            # NEVER let the model reinterpret a legal or compensation answer. Asked to map
            # "Yes" onto a Right-to-Work select, it chose "I have the right to work without
            # sponsorship" for a candidate who needs sponsorship — false, on a real application.
            del out[f["key"]]
            why[f["key"]] = (f"your saved answer {chosen!r} is not one of the options this form "
                             f"offers, and it is a legal question — pick the right option yourself")
            continue
        ask.append({**f, "_hint": chosen})
        del out[f["key"]]
        why[f["key"]] = f"your saved answer {chosen!r} matches no option — asked the model to map it"

    # 3. Nothing matched by rule — ask the model whether any of these is a REWORDING of
    #    something already answered. Recall before composition: reusing a real answer is
    #    always better than writing a new one.
    recall_targets = [f for f in ask if not f["sensitive"]]
    if recall_targets:
        rec = _semantic_recall(recall_targets, bank, episodes, trace=trace)
        for f in list(ask):
            if f["key"] in rec:
                out[f["key"]] = {"answer": rec[f["key"]], "source": "recall"}
                why[f["key"]] = "recognised as another wording of a question you have answered"
                ask.remove(f)

    if trace is not None:
        trace["answered_from_profile"] = {f["key"]: out[f["key"]] for f in schema if f["key"] in out}
        trace["sent_to_model"] = [f["label"] for f in ask]
        trace["withheld_sensitive"] = [f["label"] for f in schema
                                       if f["sensitive"] and f["key"] not in out]
        trace["left_to_human"] = [f["label"] for f in schema if f["type"] == "consent"]
        if not ask:
            trace["why_no_model_call"] = ("every required question was already answered from your "
                                          "profile, or is one only you may answer")
        elif not context:
            trace["why_no_model_call"] = "no candidate material was available to answer from"

    if ask and context:
        fields = []
        for f in ask:
            q = {"label": f["label"]}
            if f["options"]:
                q["choose_one_of"] = f["options"][:14]
            if f.get("_hint"):
                q["candidate_said"] = f["_hint"]      # map their words onto a real option
            fields.append(q)
        llm_trace = {} if trace is not None else None
        answered = ab._llm_answer_fields(context, fields, extra_prompt, trace=llm_trace,
                                         system=(bank or {}).get("_system_prompt", "")) or {}
        if trace is not None:
            trace["llm"] = llm_trace
        def _norm_q(x):
            return re.sub(r"[^a-z0-9 ]+", "", str(x or "").lower()).strip()
        by_label = {f["label"]: f for f in ask}
        by_norm = {_norm_q(f["label"]): f for f in ask}
        claimed = set()
        for label, ans in answered.items():
            f = by_label.get(label) or by_norm.get(_norm_q(label))
            if f is None:
                # A model sometimes rewords the key it echoes back, so a fuzzy match is needed —
                # but it must be TIGHT. A loose one handed the answer for "years of experience
                # overall" ("4+") to "years of hands-on Book Keeping experience", which would
                # have claimed four years of bookkeeping the candidate has never done.
                lf = ab._fp(label)
                best, bs = None, 0.0
                for cand in ask:
                    if cand["key"] in claimed or cand["label"] in answered:
                        continue                       # already has its own answer
                    cf = ab._fp(cand["label"])
                    if not cf or not lf:
                        continue
                    overlap = len(lf & cf) / max(1, min(len(lf), len(cf)))
                    if overlap > bs:
                        bs, best = overlap, cand
                f = best if bs >= 0.8 else None         # near-identical wording only
            if f and f["key"] not in claimed and str(ans).strip():
                claimed.add(f["key"])
                out[f["key"]] = {"answer": str(ans).strip(), "source": "llm"}
                why[f["key"]] = "answered by the model from your résumé and stated facts"
        for f in ask:
            if f["key"] not in out:
                why[f["key"]] = "the model had nothing to base an answer on — needs you"

    if trace is not None:
        # one row per field: what was asked, what we knew, what we did, and why
        trace["decisions"] = [{
            "label": f["label"], "type": f["type"], "required": f["required"],
            "sensitive": f["sensitive"], "options": f["options"][:10],
            "answer": (out.get(f["key"]) or {}).get("answer", ""),
            "source": (out.get(f["key"]) or {}).get("source", "unanswered"),
            "why": why.get(f["key"], ""),
        } for f in schema]
    return out


# ─────────────────────────────────────────────────────────────── filling

def fill(page, frame, schema, planned, trace=None):
    """Apply the plan. Returns (filled_labels, unanswered_required).

    The outcome is written back into trace["decisions"], because a planned answer is not the
    same as a filled field: a combobox rejects a value that matches none of its suggestions,
    and the decision table was reporting "answered from your profile" for fields the form
    still listed as missing."""
    filled, missing = [], []
    for f in schema:
        p = planned.get(f["key"])
        ans = (p or {}).get("answer")
        el, t = f["_el"], f["type"]
        done = False
        if ans:
            try:
                if t == "select":
                    done = ab._select_by_text(el, ans)
                elif t == "combo":
                    done = ab._fill_combobox(page, el, ans)
                elif t == "date":
                    d = ab._date_obj(ans)
                    done = bool(d and ab._fill_date(el, d))
                elif t in ("radio", "checkgroup"):
                    labels = [ab._radio_label(x) or "" for x in el]
                    i = ab._opt_match(ans, labels)
                    if i is None and t == "checkgroup":       # prefer an all-encompassing option
                        i = next((j for j, x in enumerate(labels)
                                  if re.search(r"\b(both|any|all|either)\b", x.lower())), None)
                    if i is not None:
                        try:
                            el[i].check(); done = True
                        except Exception:
                            el[i].click(); done = True
                elif t == "yesno":
                    want = "yes" if str(ans).strip().lower() in ("yes", "y", "true", "1") else "no"
                    for b in el:
                        if (b.inner_text() or "").strip().lower() == want:
                            b.click(); done = True; break
                elif t == "checkbox":
                    el.check(); done = True
                else:
                    el.fill(str(ans)); done = True
            except Exception:
                done = False
        if done:
            filled.append(f["label"])
        elif f["required"]:
            missing.append({"label": f["label"], "type": t, "options": f["options"][:12]})
        if trace is not None and not done and ans:
            # planned, but the control would not take it — say so rather than claiming success
            for d in trace.get("decisions", []):
                if d["label"] == f["label"]:
                    d["filled"] = False
                    d["why"] = (f"planned {str(ans)[:40]!r} from your {(p or {}).get('source','profile')}, "
                                f"but this {t} has no matching option — update it under Add answers")
                    break
        elif trace is not None:
            for d in trace.get("decisions", []):
                if d["label"] == f["label"]:
                    d["filled"] = bool(done)
                    break
    return filled, missing


# ─────────────────────────────────────────────────────────────── the whole run

def run(page, frame, vendor, bank, context="", extra_prompt="", episodes=None):
    """navigate → extract → plan → fill, returning everything for the record."""
    trace = {"agent": route(page.url or "")["name"],
             "memory": ab_memory.stats(episodes),
             "profile_facts": {k: v for k, v in (bank or {}).items()
                               if not str(k).startswith("_") and v}}
    schema = extract(frame)
    trace["fields_read"] = to_json(schema)
    planned = plan(schema, bank, context, extra_prompt, trace=trace, episodes=episodes)
    filled, missing = fill(page, frame, schema, planned, trace=trace)
    return {
        "schema": to_json(schema),
        "plan": {k: v for k, v in planned.items()},
        "filled_labels": filled,
        "unfilled_required": missing,
        "counts": {"fields": len(schema), "planned": len(planned),
                   "filled": len(filled), "missing": len(missing)},
        "trace": trace,          # the whole chain: agent, facts, fields, prompt, response
    }
