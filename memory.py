"""memory.py — what the agent remembers between applications.

A synonym list cannot keep up with how differently boards word the same question. "How soon
you can join us?", "What is your official notice period?", "Earliest start date" and "Joining
time" are one fact asked four ways, and every new board invents a fifth. So instead of adding
patterns forever, the agent remembers what it has been asked before and what was answered.

Two layers, both kept on the profile (no migration):

  facts     — profiles.data.standing: structured keys the user filled in once
  episodes  — profiles.data.answer_memory: every question the agent has actually met, with
              the answer used, where it came from, and whether the form accepted it

Episodes are what make recall work on rewordings: a question that has been seen before is
matched directly, and one that has not is matched semantically against everything remembered
(see board_agents._semantic_recall). Outcomes matter too — an answer the form rejected is
remembered as rejected, so it is not confidently reused elsewhere.
"""
import re, time

MAX_EPISODES = 400          # plenty for recall, small enough to send to a model


def _norm(q):
    return re.sub(r"[^a-z0-9 ]+", " ", str(q or "").lower()).strip()


def load(profile):
    """(facts, episodes) from a profile row."""
    data = (profile or {}).get("data") or {}
    return (data.get("standing") or {}), list(data.get("answer_memory") or [])


def remember(profile, board, decisions):
    """Fold this application's decisions into the episode list.

    `decisions` is the per-field record the planner produces: label, answer, source, and
    whether it actually landed. Returns the new episode list, newest last.
    """
    _, episodes = load(profile)
    by_norm = {_norm(e.get("q")): e for e in episodes}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for d in decisions or []:
        q, a = (d.get("label") or "").strip(), (d.get("answer") or "").strip()
        if not q:
            continue
        landed = d.get("filled")
        prev = by_norm.get(_norm(q))
        if prev is None:
            prev = {"q": q[:220], "seen": 0}
            episodes.append(prev)
            by_norm[_norm(q)] = prev
        prev["seen"] = int(prev.get("seen") or 0) + 1
        prev["last_seen"] = now
        prev["boards"] = sorted(set((prev.get("boards") or []) + ([board] if board else [])))[:8]
        prev["type"] = d.get("type") or prev.get("type") or ""
        if d.get("options"):
            prev["options"] = d["options"][:10]
        if a:
            # only remember an answer the form actually took; a rejected one is noted so it is
            # not reused with confidence somewhere else
            if landed is False:
                prev["rejected"] = a[:180]
            else:
                prev["a"] = a[:180]
                prev["source"] = d.get("source") or ""
                prev.pop("rejected", None)
    episodes.sort(key=lambda e: (e.get("last_seen") or ""), reverse=True)
    return episodes[:MAX_EPISODES]


def answered_episodes(episodes):
    """Only those with an answer that stuck — the ones worth recalling."""
    return [e for e in (episodes or []) if (e.get("a") or "").strip()]


def direct_recall(question, episodes):
    """An answer for this exact question (ignoring case and punctuation), or None."""
    n = _norm(question)
    for e in answered_episodes(episodes):
        if _norm(e.get("q")) == n:
            return e.get("a")
    return None


def stats(episodes):
    eps = episodes or []
    answered = answered_episodes(eps)
    return {"questions_seen": len(eps),
            "with_answers": len(answered),
            "reused": sum(1 for e in eps if int(e.get("seen") or 0) > 1),
            "rejected": sum(1 for e in eps if e.get("rejected"))}
