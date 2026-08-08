"""resume_build.py — build a COMPLETE résumé (HTML) server-side from a stored profile.

The background auto-applier can't reach the browser's résumé renderer, so it used to
attach a barebones stub (name + skills). This builds a full, ATS-friendly document —
summary, skills, real experience, projects, education, certifications, links — from the
profile's `data` blob, using the tailored summary when one exists. Facts come only from
the candidate's own profile; nothing is invented here.
"""
import html


def _esc(s):
    return html.escape(str(s or ""), quote=True)


def _skills_list(profile):
    sk = profile.get("skills")
    if isinstance(sk, str):
        return [s.strip() for s in sk.replace("\n", ",").split(",") if s.strip()]
    return [str(s).strip() for s in (sk or []) if str(s).strip()]


def _contact_line(profile, data):
    bits = []
    for k in ("email", "contact"):
        v = profile.get(k)
        if v and str(v).strip():
            bits.append(str(v).strip())
    if data.get("phone"):
        bits.append(str(data["phone"]).strip())
    addr = data.get("address") or {}
    if isinstance(addr, dict):
        loc = ", ".join(x for x in [addr.get("city"), addr.get("state"), addr.get("country")] if x)
        if loc:
            bits.append(loc)
    for l in (data.get("links") or []):
        u = (l or {}).get("url")
        if u:
            bits.append(str(u).replace("https://", "").replace("http://", ""))
    # de-dup while preserving order
    seen, out = set(), []
    for b in bits:
        if b and b not in seen:
            seen.add(b); out.append(b)
    return " · ".join(out)


def build_resume_html(profile, tailoring=None):
    """profile: a Supabase `profiles` row (name/email/title/contact/summary/skills + data blob).
       tailoring: optional {summary, bullets} — its summary (targeted to the job) overrides the
                  profile summary; the candidate's REAL experience is always what's listed."""
    profile = profile or {}
    data = profile.get("data") or {}
    t = tailoring or {}
    name = profile.get("name") or "Candidate"
    title = profile.get("title") or ""
    summary = (t.get("summary") or profile.get("summary") or "").strip()
    skills = _skills_list(profile)
    contact = _contact_line(profile, data)

    P = []
    P.append("<div style='font-family:Georgia,\"Times New Roman\",serif;max-width:760px;margin:0 auto;"
             "color:#1a1a1a;line-height:1.4;font-size:12.5px'>")
    P.append(f"<h1 style='margin:0 0 2px;font-size:22px'>{_esc(name)}</h1>")
    if title:
        P.append(f"<div style='font-size:13.5px;color:#444;margin-bottom:3px'>{_esc(title)}</div>")
    if contact:
        P.append(f"<div style='font-size:11px;color:#555;margin-bottom:12px'>{_esc(contact)}</div>")

    def head(txt):
        P.append(f"<h2 style='font-size:12.5px;text-transform:uppercase;letter-spacing:.06em;"
                 f"border-bottom:1px solid #bbb;padding-bottom:2px;margin:16px 0 7px'>{_esc(txt)}</h2>")

    if summary:
        head("Summary")
        P.append(f"<p style='margin:0 0 4px'>{_esc(summary)}</p>")

    if skills:
        head("Skills")
        P.append("<p style='margin:0'>" + _esc(", ".join(skills)) + "</p>")

    exp = [e for e in (data.get("exp") or []) if (e or {}).get("role") or (e or {}).get("company")
           or (e or {}).get("bullets")]
    if exp:
        head("Experience")
        for e in exp:
            role = e.get("role") or ""
            co = e.get("company") or ""
            dates = e.get("dates") or ""
            headline = " — ".join(x for x in [role, co] if x)
            P.append("<div style='margin:0 0 9px'>")
            P.append(f"<div style='display:flex;justify-content:space-between'>"
                     f"<strong>{_esc(headline)}</strong><span style='color:#666;font-size:11px'>{_esc(dates)}</span></div>")
            bullets = [b for b in (e.get("bullets") or []) if str(b).strip()]
            if bullets:
                P.append("<ul style='margin:4px 0 0;padding-left:18px'>")
                for b in bullets:
                    P.append(f"<li style='margin:1px 0'>{_esc(b)}</li>")
                P.append("</ul>")
            elif e.get("blurb"):
                P.append(f"<div style='color:#333'>{_esc(e['blurb'])}</div>")
            P.append("</div>")

    proj = [p for p in (data.get("proj") or []) if (p or {}).get("name") or (p or {}).get("desc")]
    if proj:
        head("Projects")
        for p in proj:
            nm = p.get("name") or ""
            desc = p.get("desc") or ""
            P.append(f"<div style='margin:0 0 6px'><strong>{_esc(nm)}</strong>"
                     + (f" — {_esc(desc)}" if desc else "") + "</div>")

    edu = [e for e in (data.get("education") or []) if (e or {}).get("school") or (e or {}).get("degree")]
    if edu:
        head("Education")
        for e in edu:
            line = " — ".join(x for x in [e.get("degree") or "", e.get("school") or ""] if x)
            P.append(f"<div style='display:flex;justify-content:space-between;margin:0 0 3px'>"
                     f"<span>{_esc(line)}</span><span style='color:#666;font-size:11px'>{_esc(e.get('dates') or '')}</span></div>")

    certs = [c for c in (data.get("certs") or []) if (c or {}).get("name")]
    if certs:
        head("Certifications")
        for c in certs:
            line = c.get("name") or ""
            if c.get("issuer"):
                line += f" — {c['issuer']}"
            P.append(f"<div style='margin:0 0 2px'>{_esc(line)}</div>")

    for sec in (data.get("custom_sections") or []):
        title2 = (sec or {}).get("title")
        body = (sec or {}).get("body") or (sec or {}).get("content")
        if title2 and body:
            head(title2)
            P.append(f"<div>{_esc(body)}</div>")

    P.append("</div>")
    return "<html><body>" + "".join(P) + "</body></html>"
